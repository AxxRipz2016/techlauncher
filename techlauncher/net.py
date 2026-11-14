"""Сеть: JSON-запросы и загрузка файлов (по одному с докачкой или пачкой в несколько потоков).

Используется только стандартная библиотека (urllib), поэтому работают системные
настройки прокси (переменные http_proxy/https_proxy), что важно для учебных сетей.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import ssl
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from . import LauncherError, __version__

USER_AGENT = f"techlauncher/{__version__}"
TIMEOUT = 30
CHUNK = 256 * 1024

# progress(скачано_байт, всего_байт, скачано_файлов, всего_файлов)
Progress = Optional[Callable[[int, int, int, int], None]]


def _request(url: str, data: bytes | None = None, headers: Dict[str, str] | None = None,
             method: str | None = None) -> urllib.request.Request:
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    return urllib.request.Request(url, data=data, headers=h, method=method)


def get_json(url: str, timeout: int = TIMEOUT):
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise LauncherError(f"не удалось загрузить {url}: {e}") from None


def request_json(url: str, *, json_body=None, form: Dict[str, str] | None = None,
                 headers: Dict[str, str] | None = None, timeout: int = TIMEOUT) -> Tuple[int, dict]:
    """POST (если есть тело) или GET. Возвращает (HTTP-код, разобранный JSON).

    Ошибки HTTP не бросаются: вызывающий код сам решает, что значит 401 или 404.
    """
    hdrs = {"Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    if headers:
        hdrs.update(headers)
    req = _request(url, data, hdrs, "POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    except OSError as e:
        host = urllib.parse.urlparse(url).netloc
        raise LauncherError(f"нет связи с {host}: {e}") from None
    try:
        parsed = json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        parsed = {"raw": body.decode("utf-8", "replace")[:500]}
    return status, parsed


# ------------------------------------------------------------------ файлы


@dataclass
class Download:
    url: str
    path: Path
    size: Optional[int] = None
    sha1: Optional[str] = None
    sha256: Optional[str] = None
    executable: bool = False


def hash_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def file_ok(path: Path, size: int | None = None, sha1: str | None = None,
            sha256: str | None = None, deep: bool = False) -> bool:
    """Файл уже скачан правильно?

    Обычно сравнивается только размер (быстро). Хеш считается при deep=True
    или когда размер неизвестен.
    """
    try:
        st = path.stat()
    except OSError:
        return False
    if size is not None and st.st_size != size:
        return False
    if deep or size is None:
        if sha1 and hash_file(path, "sha1") != sha1.lower():
            return False
        if sha256 and hash_file(path, "sha256") != sha256.lower():
            return False
    return True


# --- соединения keep-alive: у каждого потока свои, переиспользуются между файлами.
# Ассеты Minecraft — тысячи мелких файлов, и новое HTTPS-соединение на каждый
# (с TLS-рукопожатием) замедляет загрузку в разы.

_local = threading.local()
_ssl_ctx: Optional[ssl.SSLContext] = None


def _use_proxy(scheme: str) -> bool:
    return scheme in urllib.request.getproxies()


def _connection(scheme: str, host: str) -> http.client.HTTPConnection:
    global _ssl_ctx
    pool = getattr(_local, "pool", None)
    if pool is None:
        pool = _local.pool = {}
    conn = pool.get((scheme, host))
    if conn is None:
        if scheme == "https":
            if _ssl_ctx is None:
                _ssl_ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(host, timeout=TIMEOUT, context=_ssl_ctx)
        else:
            conn = http.client.HTTPConnection(host, timeout=TIMEOUT)
        pool[(scheme, host)] = conn
    return conn


def _drop(scheme: str, host: str) -> None:
    conn = getattr(_local, "pool", {}).pop((scheme, host), None)
    if conn is not None:
        conn.close()


def _open_keepalive(url: str, headers: Dict[str, str]):
    for _ in range(6):  # до 5 перенаправлений (GitHub отдаёт файлы через редирект)
        u = urllib.parse.urlsplit(url)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        hdrs = {"User-Agent": USER_AGENT}
        hdrs.update(headers)
        for attempt in (1, 2):  # сервер мог закрыть старое соединение — пробуем новое
            conn = _connection(u.scheme, u.netloc)
            try:
                conn.request("GET", path, headers=hdrs)
                resp = conn.getresponse()
                break
            except (http.client.HTTPException, OSError):
                _drop(u.scheme, u.netloc)
                if attempt == 2:
                    raise
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.getheader("Location") or ""
            resp.read()
            if resp.will_close:
                _drop(u.scheme, u.netloc)
            url = urllib.parse.urljoin(url, location)
            continue
        return u, resp
    raise OSError(f"слишком много перенаправлений: {url}")


def _write(resp, part: Path, start: int, on_bytes) -> None:
    if start and resp.status != 206:  # сервер не умеет докачку — с начала
        start = 0
    if start and on_bytes:
        on_bytes(start)
    with open(part, "ab" if start else "wb") as f:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            if on_bytes:
                on_bytes(len(chunk))


def _fetch(url: str, part: Path, size: int | None, on_bytes, resume: bool) -> None:
    start = part.stat().st_size if (resume and part.exists()) else 0
    if size is not None and start > size:
        start = 0
    if size is not None and start == size and start > 0:
        return
    headers = {"Range": f"bytes={start}-"} if start else {}

    scheme = urllib.parse.urlsplit(url).scheme
    if not _use_proxy(scheme):
        u, resp = _open_keepalive(url, headers)
        try:
            if resp.status == 416 and start:
                resp.read()
                return
            if resp.status >= 400:
                resp.read()
                raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.msg, None)
            _write(resp, part, start, on_bytes)
        except BaseException:
            _drop(u.scheme, u.netloc)  # состояние соединения неизвестно
            raise
        if resp.will_close:
            _drop(u.scheme, u.netloc)
        return

    # Через прокси — обычный urllib (он сам берёт настройки прокси из окружения).
    try:
        resp = urllib.request.urlopen(_request(url, headers=headers), timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 416 and start:
            return
        raise
    with resp:
        _write(resp, part, start, on_bytes)


def download_file(d: Download, on_bytes: Optional[Callable[[int], None]] = None,
                  retries: int = 3, resume: bool = True) -> Path:
    """Скачать один файл: временный .part, докачка, повторы, проверка размера и хеша."""
    if file_ok(d.path, d.size, d.sha1, d.sha256, deep=True):
        return d.path
    d.path.parent.mkdir(parents=True, exist_ok=True)
    part = d.path.with_name(d.path.name + ".part")

    error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            _fetch(d.url, part, d.size, on_bytes, resume)
            error = None
            break
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            error = e
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 404, 410):
                break
            time.sleep(attempt)
    if error is not None:
        raise LauncherError(f"не удалось скачать {d.url}: {error}")

    actual = part.stat().st_size
    if d.size is not None and actual != d.size:
        part.unlink()
        raise LauncherError(f"{d.path.name}: размер {actual}, ожидался {d.size}")
    for algo, expected in (("sha1", d.sha1), ("sha256", d.sha256)):
        if expected and hash_file(part, algo) != expected.lower():
            part.unlink()
            raise LauncherError(f"{d.path.name}: не совпал хеш {algo.upper()}")
    if d.executable and sys.platform != "win32":
        os.chmod(part, 0o755)
    os.replace(part, d.path)
    return d.path


def download_many(items: Sequence[Download], progress: Progress = None,
                  workers: int = 16, deep: bool = False) -> int:
    """Скачать недостающие файлы в несколько потоков. Возвращает число скачанных."""
    unique: Dict[Path, Download] = {}
    for d in items:
        unique.setdefault(d.path, d)
    todo = [d for d in unique.values() if not file_ok(d.path, d.size, d.sha1, d.sha256, deep)]
    if not todo:
        return 0

    total_files = len(todo)
    total_bytes = sum(d.size or 0 for d in todo)
    lock = threading.Lock()
    state = {"bytes": 0, "files": 0, "last": 0.0}

    def report(force: bool = False) -> None:
        if progress is None:
            return
        now = time.monotonic()
        if force or now - state["last"] >= 0.1:
            state["last"] = now
            progress(state["bytes"], total_bytes, state["files"], total_files)

    def on_bytes(n: int) -> None:
        with lock:
            state["bytes"] += n
            report()

    def work(d: Download) -> None:
        download_file(d, on_bytes)
        with lock:
            state["files"] += 1
            report(force=state["files"] == total_files)

    errors = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(work, d) for d in todo]
        for f in as_completed(futures):
            try:
                f.result()
            except LauncherError as e:
                errors.append(str(e))
    if errors:
        raise LauncherError(f"не удалось скачать файлов: {len(errors)}. Например: {errors[0]}")
    return total_files
