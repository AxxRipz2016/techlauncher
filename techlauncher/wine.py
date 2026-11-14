"""Wine для запуска Windows-игр на Linux, в том числе переносной (без прав администратора).

Сборки берутся из https://github.com/Kron4ek/Wine-Builds. Вариант "amd64-wow64"
запускает 32-битные программы (SCP:CB как раз такая) без 32-битных системных
библиотек, которые обычно не установлены и без sudo не ставятся.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import LauncherError, archive, net
from .paths import Layout, remove_file

REPO = "Kron4ek/Wine-Builds"
API = f"https://api.github.com/repos/{REPO}/releases"
VARIANT_RE = re.compile(r"^wine-[\d.]+(?:-rc\d+)?-amd64-wow64\.tar\.xz$")
CURRENT_FILE = "current.txt"


def needs_wine() -> bool:
    return sys.platform != "win32"


# ------------------------------------------------------------------ установка


@dataclass
class WineRelease:
    tag: str
    asset_name: str
    url: str
    size: int
    sha256: Optional[str]


def find_release(tag: str | None = None) -> WineRelease:
    try:
        data = net.get_json(f"{API}/tags/{tag}" if tag else f"{API}/latest")
    except LauncherError as e:
        raise LauncherError(
            f"{e}\nЕсли GitHub недоступен, скачайте wine-*-amd64-wow64.tar.xz со страницы "
            f"https://github.com/{REPO}/releases на другом компьютере и выполните: "
            "techlauncher wine install --file АРХИВ"
        ) from None
    assets = data.get("assets", [])
    asset = next((a for a in assets if VARIANT_RE.match(a["name"])), None)
    if asset is None:
        raise LauncherError(f"в релизе {data.get('tag_name')} нет сборки amd64-wow64")
    digest = asset.get("digest") or ""
    sha256 = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
    return WineRelease(tag=data["tag_name"], asset_name=asset["name"],
                       url=asset["browser_download_url"], size=asset["size"], sha256=sha256)


def _build_name(archive_name: str) -> str:
    for suffix in (".tar.xz", ".tar.gz", ".tar"):
        if archive_name.endswith(suffix):
            return archive_name[: -len(suffix)]
    return archive_name


def install(layout: Layout, tag: str | None = None, file: Path | None = None,
            progress: net.Progress = None, log: Callable[[str], None] = print) -> Path:
    """Скачать (или взять из file) и распаковать Wine. Возвращает путь к bin/wine."""
    if not needs_wine():
        raise LauncherError("на Windows Wine не нужен: игры запускаются напрямую")

    if file is not None:
        if not file.is_file():
            raise LauncherError(f"нет файла {file}")
        tarball, name = file, _build_name(file.name)
    else:
        rel = find_release(tag)
        log(f"Wine {rel.tag}: {rel.asset_name} ({rel.size // 2**20} МБ)")
        tarball = layout.cache_dir / "wine" / rel.asset_name
        net.download_many([net.Download(rel.url, tarball, rel.size, sha256=rel.sha256)], progress)
        name = _build_name(rel.asset_name)

    target = layout.wine_dir / name
    if target.exists():
        shutil.rmtree(target)
    layout.wine_dir.mkdir(parents=True, exist_ok=True)
    log("Распаковка Wine...")
    archive.extract(tarball, target, trusted=True)

    binary = target / "bin" / "wine"
    if not binary.is_file():
        shutil.rmtree(target, ignore_errors=True)
        raise LauncherError(f"в архиве нет bin/wine — это точно сборка Wine? ({tarball.name})")
    (layout.wine_dir / CURRENT_FILE).write_text(name, encoding="utf-8")
    if file is None:
        remove_file(tarball)
    return binary


def managed_wine(layout: Layout) -> Path | None:
    current = layout.wine_dir / CURRENT_FILE
    if not current.is_file():
        return None
    binary = layout.wine_dir / current.read_text(encoding="utf-8").strip() / "bin" / "wine"
    return binary if binary.is_file() else None


def remove(layout: Layout) -> bool:
    if not layout.wine_dir.exists():
        return False
    shutil.rmtree(layout.wine_dir)
    return True


# ------------------------------------------------------------------ запуск


def find_wine(layout: Layout, explicit: str | None = None) -> str:
    """Порядок поиска: явный путь, TECHL_WINE, Wine от лаунчера, системный wine."""
    candidate = explicit or os.environ.get("TECHL_WINE")
    if candidate:
        path = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if path is None:
            raise LauncherError(f"Wine не найден по пути {candidate}")
        return path
    managed = managed_wine(layout)
    if managed is not None:
        return str(managed)
    for name in ("wine", "wine64"):
        path = shutil.which(name)
        if path:
            return path
    raise LauncherError(
        "для запуска Windows-игр на Linux нужен Wine. Без прав администратора:\n"
        "  techlauncher wine install\n"
        "или через пакетный менеджер (нужен sudo): sudo dnf install wine / sudo apt install wine"
    )


def build_command(layout: Layout, exe: Path, args: List[str], wine: str | None = None,
                  prefix: Path | None = None) -> Tuple[List[str], Dict[str, str]]:
    """Команда запуска exe и переменные окружения (на Windows — напрямую)."""
    env = dict(os.environ)
    if not needs_wine():
        return [str(exe), *args], env
    wine_bin = find_wine(layout, wine)
    if prefix is not None:
        prefix.mkdir(parents=True, exist_ok=True)
        env["WINEPREFIX"] = str(prefix)
    env.setdefault("WINEDEBUG", "-all")
    env.setdefault("WINEDLLOVERRIDES", "mscoree,mshtml=")  # не предлагать Mono/Gecko
    env.setdefault("PULSE_LATENCY_MSEC", "60")              # меньше хрипа в звуке
    return [wine_bin, str(exe), *args], env


def ensure_video_memory(wine_bin: str, env: Dict[str, str], prefix: Path | None,
                        megabytes: int) -> bool:
    """Сообщать играм фиксированный объём видеопамяти.

    Старые игры на Blitz3D (SCP:CB) хранят объём VRAM в 32-битном знаковом числе.
    Если Wine сообщает много памяти, число становится отрицательным
    ("Total video memory (MB): -8"), и игра падает на загрузке с
    "Memory access violation". Значение пишется в реестр префикса один раз.
    """
    marker = prefix / f".techlauncher-vram-{megabytes}" if prefix else None
    if marker is not None and marker.exists():
        return False
    result = subprocess.run(
        [wine_bin, "reg", "add", r"HKCU\Software\Wine\Direct3D",
         "/v", "VideoMemorySize", "/t", "REG_SZ", "/d", str(megabytes), "/f"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        raise LauncherError(f"не удалось настроить видеопамять в Wine: {result.stderr.strip()}")
    if marker is not None and prefix is not None:
        for old in prefix.glob(".techlauncher-vram-*"):
            old.unlink()
        marker.touch()
    return True
