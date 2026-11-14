"""Проверка компьютера: что здесь получится запустить и что мешает."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from . import LauncherError, net, scpcb, wine
from .mc import java as mc_java
from .mc import versions as mc_versions
from .paths import Layout, remove_file

OK, WARN, FAIL, INFO = "OK", "!!", "XX", "--"
NEEDED_SPACE = 3 * 2**30  # Minecraft (~1 ГБ) + Java (~0.2) + SCP:CB (~0.6) + Wine (~0.8)

SITES = [
    ("Mojang (версии Minecraft)", mc_versions.MANIFEST_URL, True),
    ("Mojang (библиотеки)", "https://libraries.minecraft.net/", True),
    ("Mojang (ассеты)", "https://resources.download.minecraft.net/", True),
    ("Microsoft (вход в аккаунт)", "https://login.microsoftonline.com/", False),
    ("Сайт SCP:CB (undertowgames.com)", scpcb.BUILTIN_VERSIONS["versions"][0]["url"], True),
]
LINUX_SITES = [
    ("GitHub (загрузка Wine)", f"https://github.com/{wine.REPO}/releases", True),
    ("Flathub (dl.flathub.org)", "https://dl.flathub.org/repo/flathub.flatpakrepo", False),
]


@dataclass
class Check:
    status: str
    title: str
    detail: str = ""
    advice: str = ""


def _reachable(url: str) -> Tuple[bool, str]:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": net.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return e.code < 500 and e.code not in (403, 451), f"HTTP {e.code}"
    except OSError as e:
        return False, str(getattr(e, "reason", e))


def _exec_allowed(directory: Path) -> Tuple[bool, str]:
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="techl-test-", suffix=".sh", dir=str(directory))
    path = Path(name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write("#!/bin/sh\necho ok\n")
        os.chmod(path, 0o755)
        out = subprocess.run([str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, timeout=10)
        return out.stdout.strip() == "ok", (out.stderr.strip() or "ok")
    except OSError as e:
        return False, str(e)
    finally:
        remove_file(path)


def _distro() -> str:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"{platform.system()} {platform.release()}"
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    name = fields.get("PRETTY_NAME", fields.get("NAME", "Linux")).strip().strip('"')
    return f"{name} (ядро {platform.release()})"


def _flatpak() -> Check:
    title = "Flatpak (для Steam/Sober)"
    exe = shutil.which("flatpak")
    if exe is None:
        return Check(INFO, title, "не установлен",
                     "без прав администратора не поставить; Minecraft и SCP:CB это не мешает")
    try:
        ver = subprocess.run([exe, "--version"], stdout=subprocess.PIPE,
                             universal_newlines=True, timeout=20).stdout.strip()
        remotes = subprocess.run([exe, "remotes", "--columns=name,options"], stdout=subprocess.PIPE,
                                 universal_newlines=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return Check(WARN, title, f"{exe}: {e}", "Flatpak есть, но не запускается")
    flathub = [line.split() for line in remotes.splitlines() if line.lower().startswith("flathub")]
    if flathub:
        where = ", ".join(r[1] if len(r) > 1 else "?" for r in flathub)
        return Check(OK, title, f"{ver}; Flathub подключён ({where})")
    return Check(OK, title, f"{ver}; Flathub не подключён",
                 "подключить для себя без sudo: flatpak remote-add --user --if-not-exists "
                 "flathub https://dl.flathub.org/repo/flathub.flatpakrepo")


def run(layout: Layout) -> List[Check]:
    checks: List[Check] = []
    linux = sys.platform.startswith("linux")
    machine = platform.machine().lower()

    system = _distro() if linux else f"{platform.system()} {platform.release()}"
    checks.append(Check(OK, "Система", f"{system}, {machine}"))

    py_ok = sys.version_info >= (3, 7)
    checks.append(Check(OK if py_ok else FAIL, "Python", platform.python_version(),
                        "" if py_ok else "нужен Python 3.7 или новее"))
    try:
        import tkinter  # noqa: F401
        checks.append(Check(OK, "Окно лаунчера (tkinter)", "есть"))
    except ImportError:
        checks.append(Check(INFO, "Окно лаунчера (tkinter)", "нет",
                            "в этом Python окна нет (пакет python3-tkinter ставится только с sudo). "
                            "Запустите лаунчер через start.py — он сам скачает переносной Python "
                            "с окном. Команды в терминале работают и так"))

    try:
        layout.main_dir.mkdir(parents=True, exist_ok=True)
        probe = layout.main_dir / ".techl-write-test"
        probe.write_text("x")
        probe.unlink()
        free = shutil.disk_usage(str(layout.main_dir)).free
        checks.append(Check(OK if free >= NEEDED_SPACE else WARN, "Папка лаунчера",
                            f"{layout.main_dir} (свободно {free / 2**30:.1f} ГБ)",
                            "" if free >= NEEDED_SPACE else "нужно около 3 ГБ для обеих игр"))
    except OSError as e:
        checks.append(Check(FAIL, "Папка лаунчера", f"{layout.main_dir}: {e}",
                            "нет прав на запись; укажите другую: --main-dir ПУТЬ"))
        return checks

    if linux:
        ok, detail = _exec_allowed(layout.main_dir)
        checks.append(Check(OK if ok else FAIL, "Запуск программ из папки лаунчера", detail,
                            "" if ok else "запуск запрещён (noexec или политика безопасности); "
                            "попробуйте --main-dir на флешке"))

    plat = mc_java.platform_key()
    checks.append(Check(OK if plat else WARN, "Java для Minecraft",
                        f"Mojang: платформа {plat}" if plat else "Mojang не выпускает Java для этой ОС",
                        "" if plat else "укажите свою Java: techlauncher mc start --java ПУТЬ"))

    if linux:
        try:
            wine_bin = wine.find_wine(layout)
            out = subprocess.run([wine_bin, "--version"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
            ver = (out.stdout or out.stderr).strip()
            checks.append(Check(OK if out.returncode == 0 else FAIL, "Wine (для SCP:CB)",
                                f"{ver} ({wine_bin})",
                                "" if out.returncode == 0 else "Wine не запускается"))
        except LauncherError:
            checks.append(Check(WARN, "Wine (для SCP:CB)", "не найден",
                                "выполните: techlauncher wine install (sudo не нужен)"))
        except (OSError, subprocess.TimeoutExpired) as e:
            checks.append(Check(FAIL, "Wine (для SCP:CB)", str(e), "Wine не запускается"))
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            checks.append(Check(WARN, "Графический сеанс", "нет DISPLAY/WAYLAND_DISPLAY",
                                "запускайте из терминала на рабочем столе, не по SSH"))
        checks.append(_flatpak())

    for title, url, important in SITES + (LINUX_SITES if linux else []):
        ok, detail = _reachable(url)
        checks.append(Check(OK if ok else (WARN if important else INFO), title, detail,
                            "" if ok else "сайт недоступен из этой сети"))

    mc_installed = mc_versions.installed(layout)
    checks.append(Check(INFO if not mc_installed else OK, "Minecraft",
                        ", ".join(mc_installed) if mc_installed else "не установлен"))
    scp = scpcb.list_instances(layout)
    checks.append(Check(INFO if not scp else OK, "SCP:CB",
                        ", ".join(i.name for i in scp) if scp else "не установлена"))
    return checks
