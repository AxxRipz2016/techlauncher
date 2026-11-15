"""Wine для запуска Windows-игр на Linux, в том числе переносной (без прав администратора).

Сборки берутся из https://github.com/Kron4ek/Wine-Builds. Вариант "amd64-wow64"
запускает 32-битные программы (SCP:CB как раз такая) без 32-битных системных
библиотек, которые обычно не установлены и без sudo не ставятся.

Что делает модуль, кроме скачивания:
- держит переносной Wine отдельно от системного (WINELOADER/WINESERVER), чтобы
  они не путались, если на компьютере стоит и тот и другой;
- готовит папку настроек Wine (префикс) заранее и с понятным сообщением, а не
  молча при первом запуске игры;
- не даёт Wine трогать домашнюю папку: ярлыки в меню не создаются, а «Рабочий
  стол» и «Документы» внутри Wine — обычные папки, а не ссылки на ваши;
- умеет закрыть зависшую игру (wineserver -k) и сбросить префикс.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import LauncherError, archive, net
from .paths import Layout, load_json, remove_file, save_json

REPO = "Kron4ek/Wine-Builds"
API = f"https://api.github.com/repos/{REPO}/releases"
VARIANT_RE = re.compile(r"^wine-[\d.]+(?:-rc\d+)?-amd64-wow64\.tar\.xz$")
CURRENT_FILE = "current.txt"
PREFIX_STATE = ".techlauncher.json"

# mscoree/mshtml: не предлагать скачать Mono и Gecko; winemenubuilder: не создавать
# ярлыки и ассоциации файлов в домашней папке пользователя.
DLL_OVERRIDES = "mscoree,mshtml=;winemenubuilder.exe=d"


def needs_wine() -> bool:
    return sys.platform != "win32"


# ------------------------------------------------------------------ какой Wine


@dataclass
class Wine:
    loader: str                    # bin/wine
    server: Optional[str] = None   # bin/wineserver той же сборки
    managed: bool = False          # скачан лаунчером
    _version: Optional[str] = field(default=None, repr=False)

    @property
    def kind(self) -> str:
        return "переносной" if self.managed else "системный"

    def version(self) -> str:
        if self._version is None:
            try:
                out = subprocess.run([self.loader, "--version"], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise LauncherError(f"Wine не запускается ({self.loader}): {e}") from None
            text = (out.stdout or out.stderr).strip()
            if out.returncode != 0 or not text.startswith("wine"):
                raise LauncherError(f"Wine не запускается ({self.loader}): {text[-300:]}")
            self._version = text.splitlines()[0]
        return self._version

    def env(self, prefix: Path | None) -> Dict[str, str]:
        env = dict(os.environ)
        if prefix is not None:
            env["WINEPREFIX"] = str(prefix)
        env.setdefault("WINEDEBUG", "-all")
        env.setdefault("WINEDLLOVERRIDES", DLL_OVERRIDES)
        env.setdefault("PULSE_LATENCY_MSEC", "60")  # меньше хрипа в звуке
        if self.managed:
            # Без этого программы внутри Wine могут подхватить системный wine/wineserver
            # другой версии ("version mismatch").
            bin_dir = str(Path(self.loader).parent)
            env["WINELOADER"] = self.loader
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            if self.server:
                env["WINESERVER"] = self.server
        return env

    def run(self, args: List[str], env: Dict[str, str], timeout: int = 120) -> Tuple[int, str]:
        """Выполнить wine-команду и дождаться её. Возвращает (код, вывод).

        Вывод идёт во временный файл, а не в канал: wineserver и службы Wine
        остаются работать в фоне и держали бы канал открытым, и ожидание
        конца вывода зависло бы.
        """
        with tempfile.TemporaryFile() as out:
            try:
                code = subprocess.run([self.loader, *args], env=env, stdin=subprocess.DEVNULL,
                                      stdout=out, stderr=subprocess.STDOUT, timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                raise LauncherError(f"Wine не ответил за {timeout} с ({' '.join(args)}). "
                                    "Попробуйте: techlauncher wine kill, затем ещё раз") from None
            except OSError as e:
                raise LauncherError(f"Wine не запускается: {e}") from None
            out.seek(0)
            return code, out.read().decode("utf-8", "replace")


def _sibling_server(loader: str) -> Optional[str]:
    path = Path(loader).parent / "wineserver"
    return str(path) if path.is_file() else None


def managed_wine(layout: Layout) -> Path | None:
    current = layout.wine_dir / CURRENT_FILE
    if not current.is_file():
        return None
    binary = layout.wine_dir / current.read_text(encoding="utf-8").strip() / "bin" / "wine"
    return binary if binary.is_file() else None


def resolve(layout: Layout, explicit: str | None = None) -> Wine:
    """Порядок поиска: явный путь, TECHL_WINE, Wine от лаунчера, системный wine."""
    candidate = explicit or os.environ.get("TECHL_WINE")
    if candidate:
        path = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if path is None:
            raise LauncherError(f"Wine не найден по пути {candidate}")
        return Wine(path, _sibling_server(path))
    managed = managed_wine(layout)
    if managed is not None:
        return Wine(str(managed), _sibling_server(str(managed)), managed=True)
    for name in ("wine", "wine64"):
        path = shutil.which(name)
        if path:
            return Wine(path, _sibling_server(path) or shutil.which("wineserver"))
    raise LauncherError(
        "для запуска Windows-игр на Linux нужен Wine. Без прав администратора:\n"
        "  techlauncher wine install\n"
        "или через пакетный менеджер (нужен sudo): sudo dnf install wine / sudo apt install wine"
    )


def find_wine(layout: Layout, explicit: str | None = None) -> str:
    return resolve(layout, explicit).loader


# ------------------------------------------------------------------ установка


@dataclass
class WineRelease:
    tag: str
    asset_name: str
    url: str
    size: int
    sha256: Optional[str]


def _pick_asset(release: dict) -> Optional[dict]:
    return next((a for a in release.get("assets", []) if VARIANT_RE.match(a["name"])), None)


def find_release(tag: str | None = None) -> WineRelease:
    """Нужный релиз; без tag — самый свежий стабильный, в котором есть сборка amd64-wow64."""
    try:
        if tag:
            releases = [net.get_json(f"{API}/tags/{tag}")]
        else:
            releases = net.get_json(f"{API}?per_page=20")
    except LauncherError as e:
        raise LauncherError(
            f"{e}\nЕсли GitHub недоступен, скачайте wine-*-amd64-wow64.tar.xz со страницы "
            f"https://github.com/{REPO}/releases на другом компьютере и выполните: "
            "techlauncher wine install --file АРХИВ"
        ) from None
    usable = [(r, a) for r in releases if not r.get("draft") for a in [_pick_asset(r)] if a]
    # Предпочитаем стабильные версии, а не кандидаты (-rc) и пре-релизы.
    stable = [(r, a) for r, a in usable if not r.get("prerelease") and "-rc" not in a["name"]]
    chosen = (stable or usable or [None])[0]
    if chosen is None:
        raise LauncherError("в релизах не нашлось сборки amd64-wow64"
                            + (f" (версия {tag})" if tag else ""))
    rel, asset = chosen
    digest = asset.get("digest") or ""
    sha256 = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
    return WineRelease(tag=rel["tag_name"], asset_name=asset["name"],
                       url=asset["browser_download_url"], size=asset["size"], sha256=sha256)


def _build_name(archive_name: str) -> str:
    for suffix in (".tar.xz", ".tar.gz", ".tar"):
        if archive_name.endswith(suffix):
            return archive_name[: -len(suffix)]
    return archive_name


def _current_name(layout: Layout) -> Optional[str]:
    current = layout.wine_dir / CURRENT_FILE
    return current.read_text(encoding="utf-8").strip() if current.is_file() else None


def install(layout: Layout, tag: str | None = None, file: Path | None = None,
            progress: net.Progress = None, log: Callable[[str], None] = print,
            force: bool = False) -> Path:
    """Скачать (или взять из file) и распаковать Wine. Возвращает путь к bin/wine.

    Если эта сборка уже стоит — ничего не качает (это и есть обновление: команда
    скачает новую версию, только если она вышла). Старая сборка удаляется только
    после того, как новая запустилась.
    """
    if not needs_wine():
        raise LauncherError("на Windows Wine не нужен: игры запускаются напрямую")

    if file is not None:
        if not file.is_file():
            raise LauncherError(f"нет файла {file}")
        rel, tarball, name = None, file, _build_name(file.name)
    else:
        rel = find_release(tag)
        name = _build_name(rel.asset_name)
        tarball = layout.cache_dir / "wine" / rel.asset_name

    old_name = _current_name(layout)
    installed = managed_wine(layout)
    if old_name == name and installed is not None and not force:
        log(f"Wine {name} уже установлен — это последняя версия")
        return installed

    if rel is not None:
        log(f"Wine {rel.tag}: {rel.asset_name} ({rel.size // 2**20} МБ)")
        net.download_many([net.Download(rel.url, tarball, rel.size, sha256=rel.sha256)], progress)

    target = layout.wine_dir / name
    if target.exists():
        shutil.rmtree(target)
    layout.wine_dir.mkdir(parents=True, exist_ok=True)
    log("Распаковка Wine...")
    archive.extract(tarball, target, trusted=True)

    binary = target / "bin" / "wine"
    try:
        if not binary.is_file():
            raise LauncherError(f"в архиве нет bin/wine — это точно сборка Wine? ({tarball.name})")
        version = Wine(str(binary), _sibling_server(str(binary)), managed=True).version()
    except LauncherError:
        shutil.rmtree(target, ignore_errors=True)
        raise
    log(f"Проверка: {version} запускается")

    # Старый wineserver той же папки настроек мешал бы новой версии ("version mismatch").
    if installed is not None and old_name != name and layout.wine_prefix.exists():
        old = Wine(str(installed), _sibling_server(str(installed)), managed=True)
        kill(old, layout.wine_prefix)

    (layout.wine_dir / CURRENT_FILE).write_text(name, encoding="utf-8")
    for other in layout.wine_dir.iterdir():
        if other.is_dir() and other.name != name:
            shutil.rmtree(other, ignore_errors=True)
            log(f"Удалена старая сборка: {other.name}")
    if file is None:
        remove_file(tarball)
    return binary


def remove(layout: Layout) -> bool:
    if not layout.wine_dir.exists():
        return False
    managed = managed_wine(layout)
    if managed is not None and layout.wine_prefix.exists():
        kill(Wine(str(managed), _sibling_server(str(managed)), managed=True), layout.wine_prefix)
    shutil.rmtree(layout.wine_dir)
    return True


# ------------------------------------------------------------------ префикс (папка настроек)


def kill(w: Wine, prefix: Path) -> bool:
    """Закрыть все программы Wine в этом префиксе (зависшую игру тоже)."""
    env = w.env(prefix)
    server = w.server or shutil.which("wineserver")
    try:
        if server:
            r = subprocess.run([server, "-k"], env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30)
        else:
            r = subprocess.run([w.loader, "wineboot", "-k"], env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def reset_prefix(layout: Layout, prefix: Path | None = None, w: Wine | None = None) -> bool:
    """Удалить папку настроек Wine; при следующем запуске она создастся заново.

    Сохранения SCP:CB лежат в папке игры (Saves/), их это не затрагивает.
    """
    prefix = prefix or layout.wine_prefix
    if not prefix.exists():
        return False
    if w is None:
        try:
            w = resolve(layout)
        except LauncherError:
            w = None
    if w is not None:
        kill(w, prefix)
    shutil.rmtree(prefix)
    return True


def sandbox(prefix: Path) -> int:
    """Заменить ссылки «Рабочий стол», «Документы» и т. п. внутри Wine обычными папками.

    По умолчанию Wine связывает их с папками в домашнем каталоге, и игры
    складывают туда свои файлы. Возвращает число заменённых ссылок.
    """
    users = prefix / "drive_c" / "users"
    count = 0
    if not users.is_dir():
        return 0
    for user in users.iterdir():
        if not user.is_dir() or user.is_symlink():
            continue
        for entry in user.iterdir():
            if entry.is_symlink():
                entry.unlink()
                entry.mkdir()
                count += 1
    return count


def prefix_state(prefix: Path) -> dict:
    return load_json(prefix / PREFIX_STATE, {}) or {}


def prepare_prefix(w: Wine, env: Dict[str, str], prefix: Path, vram: int = 0,
                   log: Callable[[str], None] = print) -> None:
    """Подготовить префикс перед запуском игры (быстро, если всё уже сделано)."""
    state = prefix_state(prefix)
    version = w.version()
    fresh = not (prefix / "system.reg").is_file()
    if fresh or state.get("wine") != version:
        if fresh:
            log("Wine: создаю папку настроек, это займёт около минуты (только в первый раз)...")
        elif state.get("wine"):
            log(f"Wine обновился ({version}): обновляю папку настроек...")
        else:
            log("Wine: проверяю папку настроек...")
        prefix.mkdir(parents=True, exist_ok=True)
        code, output = w.run(["wineboot", "-u"], env, timeout=600)
        if code != 0 or not (prefix / "system.reg").is_file():
            raise LauncherError("Wine не смог подготовить папку настроек:\n"
                                + output.strip()[-800:]
                                + "\nПопробуйте сбросить её: techlauncher wine reset")
        state = {"wine": version}

    replaced = sandbox(prefix)
    if replaced:
        log(f"Wine: {replaced} папок внутри Wine отвязаны от домашнего каталога")

    if vram > 0 and state.get("vram") != vram:
        set_video_memory(w, env, vram)
        state["vram"] = vram
        log(f"Wine: игре сообщается {vram} МБ видеопамяти (защита от вылета при загрузке)")

    for old in prefix.glob(".techlauncher-vram-*"):  # метки старых версий лаунчера
        remove_file(old)
    save_json(prefix / PREFIX_STATE, state)


def set_video_memory(w: Wine, env: Dict[str, str], megabytes: int) -> None:
    """Сообщать играм фиксированный объём видеопамяти.

    Старые игры на Blitz3D (SCP:CB) хранят объём VRAM в 32-битном знаковом числе.
    Если Wine сообщает много памяти, число становится отрицательным
    ("Total video memory (MB): -8"), и игра падает на загрузке с
    "Memory access violation".
    """
    code, output = w.run(["reg", "add", r"HKCU\Software\Wine\Direct3D", "/v", "VideoMemorySize",
                          "/t", "REG_SZ", "/d", str(megabytes), "/f"], env)
    if code != 0:
        raise LauncherError(f"не удалось настроить видеопамять в Wine: {output.strip()[-300:]}")


# ------------------------------------------------------------------ запуск


def build_command(layout: Layout, exe: Path, args: List[str], wine: str | None = None,
                  prefix: Path | None = None) -> Tuple[List[str], Dict[str, str]]:
    """Команда запуска exe и переменные окружения (на Windows — напрямую)."""
    if not needs_wine():
        return [str(exe), *args], dict(os.environ)
    w = resolve(layout, wine)
    if prefix is not None:
        prefix.mkdir(parents=True, exist_ok=True)
    return [w.loader, str(exe), *args], w.env(prefix)


def status(layout: Layout, explicit: str | None = None) -> List[Tuple[str, str]]:
    """Сведения для `wine status`: пары (что, значение)."""
    rows: List[Tuple[str, str]] = []
    w = resolve(layout, explicit)
    rows.append(("Wine", f"{w.version()} ({w.kind})"))
    rows.append(("Путь", w.loader))
    prefix = layout.wine_prefix
    if (prefix / "system.reg").is_file():
        st = prefix_state(prefix)
        rows.append(("Папка настроек", str(prefix)))
        made = st.get("wine")
        if made and made != w.version():
            rows.append(("", f"создана в {made}, обновится при следующем запуске"))
        if st.get("vram"):
            rows.append(("Видеопамять для игр", f"{st['vram']} МБ"))
    else:
        rows.append(("Папка настроек", "ещё не создана (появится при первом запуске игры)"))
    return rows
