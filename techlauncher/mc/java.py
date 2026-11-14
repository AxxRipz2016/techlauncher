"""Java от Mojang: скачивается в папку лаунчера, права администратора не нужны.

Каждой версии Minecraft нужна своя Java (поле javaVersion.component в JSON):
"jre-legacy" (Java 8) для старых, "java-runtime-delta" (Java 21) для новых и т.д.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Callable, List

from .. import LauncherError, net
from ..paths import Layout, load_json, save_json
from . import rules

# Адрес взят из PortableMC (portablemc/src/base/mod.rs, JVM_META_MANIFEST_URL).
RUNTIMES_URL = ("https://piston-meta.mojang.com/v1/products/java-runtime/"
                "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json")


def platform_key() -> str | None:
    name, arch = rules.os_name(), rules.os_arch()
    return {
        ("linux", "x86_64"): "linux",
        ("linux", "x86"): "linux-i386",
        ("windows", "x86_64"): "windows-x64",
        ("windows", "x86"): "windows-x86",
        ("windows", "arm64"): "windows-arm64",
        ("osx", "x86_64"): "mac-os",
        ("osx", "arm64"): "mac-os-arm64",
    }.get((name, arch))


def java_exe(home: Path) -> Path:
    if sys.platform == "win32":
        return home / "bin" / "java.exe"
    if sys.platform == "darwin":
        return home / "jre.bundle" / "Contents" / "Home" / "bin" / "java"
    return home / "bin" / "java"


def component_for(merged: dict) -> str:
    return (merged.get("javaVersion") or {}).get("component") or "jre-legacy"


def ensure(layout: Layout, component: str, progress: net.Progress = None,
           log: Callable[[str], None] = print) -> Path:
    """Скачать (если нужно) Java нужного компонента. Возвращает путь к java."""
    home = layout.java_dir / component
    marker = home / ".techlauncher.json"
    exe = java_exe(home)

    plat = platform_key()
    if plat is None:
        raise LauncherError(f"Mojang не выпускает Java для {rules.os_name()} {rules.os_arch()}; "
                            "укажите свою: --java /путь/к/java")
    try:
        everything = net.get_json(RUNTIMES_URL)
    except LauncherError:
        if exe.is_file():  # нет интернета, но Java уже скачана
            return exe
        raise
    entries = (everything.get(plat) or {}).get(component) or []
    if not entries:
        raise LauncherError(f"у Mojang нет Java '{component}' для платформы {plat}; "
                            "укажите свою: --java /путь/к/java")
    info = entries[0]["manifest"]
    done = load_json(marker, {}) or {}
    if done.get("sha1") == info["sha1"] and exe.is_file():
        return exe

    log(f"Скачивается Java ({component}, {entries[0].get('version', {}).get('name', '?')})...")
    files = net.get_json(info["url"]).get("files", {})
    downloads: List[net.Download] = []
    links = []
    for rel, f in files.items():
        target = home / rel
        kind = f.get("type")
        if kind == "directory":
            target.mkdir(parents=True, exist_ok=True)
        elif kind == "file":
            raw = f["downloads"]["raw"]
            downloads.append(net.Download(raw["url"], target, raw.get("size"), raw.get("sha1"),
                                          executable=bool(f.get("executable"))))
        elif kind == "link":
            links.append((target, f["target"]))
    net.download_many(downloads, progress)

    for link, link_target in links:
        source = (link.parent / link_target)
        if link.exists() or link.is_symlink():
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(link_target, link)
        except OSError:  # Windows без прав на ссылки — просто копируем
            if source.is_file():
                shutil.copyfile(source, link)
    if sys.platform != "win32":  # на случай, если файл был скачан раньше без прав запуска
        for d in downloads:
            if d.executable and d.path.exists():
                os.chmod(d.path, 0o755)

    if not exe.is_file():
        raise LauncherError(f"после установки Java не найден {exe}")
    save_json(marker, {"sha1": info["sha1"], "component": component})
    return exe
