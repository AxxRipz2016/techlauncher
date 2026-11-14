#!/usr/bin/env python3
"""Запускалка TechLauncher с автообновлением.

Достаточно одного этого файла: при запуске он проверяет GitHub, скачивает свежую
версию лаунчера (если она вышла) и запускает её. Без интернета запускается уже
скачанная версия.

    python3 start.py                 # окно лаунчера
    python3 start.py doctor          # любые команды лаунчера
    python3 start.py --no-update ... # не проверять обновления

Скачать саму запускалку:
    https://raw.githubusercontent.com/AxxRipz2016/techlauncher/main/start.py

Только стандартная библиотека, Python 3.7+.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = os.environ.get("TECHL_REPO", "AxxRipz2016/techlauncher")
BRANCH = os.environ.get("TECHL_BRANCH", "main")
USER_AGENT = "techlauncher-start"


def main_dir() -> Path:
    """Та же папка, что у лаунчера (TECHL_MAIN_DIR или стандартная для ОС)."""
    env = os.environ.get("TECHL_MAIN_DIR")
    if env:
        return Path(env).expanduser().absolute()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / "techlauncher"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "techlauncher"


def say(msg: str) -> None:
    print(f"[start] {msg}", flush=True)


def latest_commit() -> str:
    url = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))["sha"]


def download(sha: str, app: Path) -> None:
    """Скачать архив коммита и аккуратно подменить им текущую версию."""
    url = f"https://codeload.github.com/{REPO}/zip/{sha}"
    tmp_zip = app / f"download-{sha[:12]}.zip"
    new_dir = app / f"new-{sha[:12]}"
    app.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp_zip, "wb") as out:
        shutil.copyfileobj(resp, out)

    if new_dir.exists():
        shutil.rmtree(new_dir)
    new_dir.mkdir()
    root = new_dir.resolve()
    with zipfile.ZipFile(tmp_zip) as zf:
        for info in zf.infolist():
            # в архиве всё лежит в папке "techlauncher-<sha>/", её убираем
            parts = info.filename.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            target = (new_dir / parts[1]).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"подозрительный путь в архиве: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    tmp_zip.unlink()

    if not (new_dir / "techlauncher" / "__init__.py").is_file():
        shutil.rmtree(new_dir)
        raise RuntimeError("в скачанном архиве нет лаунчера")
    (new_dir / "COMMIT").write_text(sha, encoding="utf-8")

    current, old = app / "current", app / "old"
    if old.exists():
        shutil.rmtree(old)
    if current.exists():
        current.rename(old)
    new_dir.rename(current)
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)


def update(app: Path) -> None:
    current = app / "current"
    have = (current / "COMMIT").read_text(encoding="utf-8").strip() \
        if (current / "COMMIT").is_file() else None
    try:
        sha = latest_commit()
    except (OSError, ValueError, KeyError) as e:
        if have:
            say(f"нет связи с GitHub ({e}), запускаю скачанную версию {have[:7]}")
            return
        raise SystemExit(f"[start] не удалось связаться с GitHub ({e}), а лаунчер ещё не скачан.\n"
                         f"Скачайте вручную: https://github.com/{REPO}")
    if sha == have:
        return
    say(("обновление " + have[:7] + " -> " if have else "скачивание лаунчера, версия ") + sha[:7])
    try:
        download(sha, app)
    except (OSError, RuntimeError, zipfile.BadZipFile) as e:
        if have:
            say(f"не удалось обновиться ({e}), запускаю версию {have[:7]}")
            return
        raise SystemExit(f"[start] не удалось скачать лаунчер: {e}")


# ------------------------------------------------------------------ переносной Python с окном
#
# tkinter нельзя поставить через pip: это часть самого Python (модуль на C и
# библиотеки Tcl/Tk), а в Linux его выносят в системный пакет python3-tkinter,
# который ставится только с sudo. Поэтому, если окна нет, скачиваем готовый
# Python со встроенным tkinter: https://github.com/astral-sh/python-build-standalone

PBS_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
PBS_RE = re.compile(r"^cpython-3\.12\.\d+\+\d+-x86_64-unknown-linux-gnu-install_only_stripped\.tar\.gz$")
COMMANDS = {"doctor", "mc", "scp", "wine", "-h", "--help", "--version"}


def wants_gui(args) -> bool:
    return "gui" in args or not any(a in COMMANDS for a in args)


def has_tkinter() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


def portable_python(root: Path) -> Path:
    return root / "python" / "bin" / "python3"


def ensure_portable_python(root: Path):
    """Скачать переносной Python с tkinter. Возвращает путь к нему или None."""
    import platform
    if not sys.platform.startswith("linux") or platform.machine().lower() not in ("x86_64", "amd64"):
        return None
    exe = portable_python(root)
    if exe.is_file():
        return exe
    say("в этом Python нет tkinter, поэтому окно не откроется. Скачиваю переносной Python "
        "с окном (~33 МБ, права администратора не нужны)...")
    try:
        req = urllib.request.Request(PBS_API, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode("utf-8"))
        asset = next(a for a in release["assets"] if PBS_RE.match(a["name"]))
        expected = (asset.get("digest") or "").split(":", 1)[-1]

        tmp = root / "python-download.tar.gz"
        root.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": USER_AGENT})
        h = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
            for chunk in iter(lambda: resp.read(1024 * 1024), b""):
                h.update(chunk)
                out.write(chunk)
        if expected and h.hexdigest() != expected:
            tmp.unlink()
            raise RuntimeError("не совпала контрольная сумма SHA-256")

        staging = root / "python.new"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        with tarfile.open(tmp) as tf:
            for m in tf.getmembers():
                target = (staging / m.name).resolve()
                if staging.resolve() not in target.parents:
                    raise RuntimeError(f"подозрительный путь в архиве: {m.name}")
            if hasattr(tarfile, "data_filter"):
                tf.extractall(staging, filter="data")
            else:
                tf.extractall(staging)
        tmp.unlink()
        if (root / "python").exists():
            shutil.rmtree(root / "python")
        (staging / "python").rename(root / "python")
        shutil.rmtree(staging)
    except (OSError, RuntimeError, StopIteration, KeyError, ValueError, tarfile.TarError) as e:
        say(f"не удалось скачать переносной Python ({e}). Окна не будет, но все команды "
            "работают: python3 start.py --help")
        return None

    check = subprocess.run([str(exe), "-c", "import tkinter"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, universal_newlines=True)
    if check.returncode != 0:
        say(f"в скачанном Python tkinter не работает: {check.stderr.strip()[-200:]}")
        return None
    say(f"переносной Python готов: {exe}")
    return exe


def main() -> int:
    if sys.version_info < (3, 7):
        print("Нужен Python 3.7 или новее")
        return 1
    args = sys.argv[1:]
    no_update = "--no-update" in args or os.environ.get("TECHL_NO_UPDATE") == "1"
    args = [a for a in args if a != "--no-update"]

    root = main_dir()
    app = root / "app"
    if not no_update:
        update(app)
    current = app / "current"

    # Окно без tkinter не откроется — перезапускаемся через переносной Python.
    if wants_gui(args) and not has_tkinter() and os.environ.get("TECHL_PORTABLE_PY") != "1":
        exe = ensure_portable_python(root)
        if exe is not None:
            os.environ["TECHL_PORTABLE_PY"] = "1"
            script = str(Path(__file__).resolve())
            os.execv(str(exe), [str(exe), script, "--no-update", *args])
    if not (current / "techlauncher" / "__init__.py").is_file():
        print(f"[start] лаунчер не найден в {current}; запустите без --no-update")
        return 1

    sys.path.insert(0, str(current))
    from techlauncher.cli import main as launcher_main
    return launcher_main(args)


if __name__ == "__main__":
    sys.exit(main())
