"""SCP: Containment Breach: установка, моды, настройки options.ini, запуск."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import LauncherError, archive, net, wine
from .ini import IniFile
from .paths import Layout, load_json, remove_file

META_FILE = "techlauncher-instance.json"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PRESERVE_ON_REINSTALL = ("Saves", "options.ini")
EXE_BLACKLIST = ("unins", "setup", "vcredist", "dxsetup", "dxwebsetup", "mapcreator",
                 "map creator", "crashreport", "roommesh", "lightmap")

# Встроенный список версий. Свои версии можно добавить в <main-dir>/scpcb/versions.json.
BUILTIN_VERSIONS = {
    "aliases": {"latest": "vanilla-1.3.11", "vanilla": "vanilla-1.3.11"},
    "versions": [
        {
            "id": "vanilla-1.3.11",
            "name": "SCP - Containment Breach v1.3.11",
            "kind": "vanilla",
            "url": "https://undertowgames.com/file/scp/SCP%20-%20Containment%20Breach%20v1.3.11.zip",
            "size": 246972862,
            "sha256": "ce0bbfa5baacf095873734377b33fa55d31ee098516e03c6371d9dc986a53000",
            "exe": "SCP - Containment Breach.exe",
            "source": "https://www.scpcbgame.com/",
            "license": "CC BY-SA 3.0",
        }
    ],
}

RESOLUTIONS = ["1024x768", "1280x720", "1280x1024", "1366x768", "1600x900", "1920x1080"]


# ------------------------------------------------------------------ версии


@dataclass
class Version:
    id: str
    name: str
    url: str
    kind: str = "mod"
    size: Optional[int] = None
    sha256: Optional[str] = None
    exe: Optional[str] = None
    source: Optional[str] = None
    license: Optional[str] = None

    @property
    def archive_name(self) -> str:
        from urllib.parse import unquote, urlparse
        return unquote(urlparse(self.url).path.rsplit("/", 1)[-1]) or f"{self.id}.zip"


def versions(layout: Layout) -> Tuple[Dict[str, Version], Dict[str, str]]:
    result: Dict[str, Version] = {}
    aliases: Dict[str, str] = {}
    for data in (BUILTIN_VERSIONS, load_json(layout.scp_user_manifest, {}) or {}):
        for raw in data.get("versions", []):
            known = {k: raw.get(k) for k in Version.__dataclass_fields__ if k in raw}
            try:
                v = Version(**known)
            except TypeError:
                raise LauncherError(f"ошибка в {layout.scp_user_manifest}: у версии нет id/name/url")
            result[v.id] = v
        aliases.update(data.get("aliases", {}))
    return result, aliases


def resolve_version(layout: Layout, name: str) -> Version:
    vs, aliases = versions(layout)
    real = aliases.get(name, name)
    if real not in vs:
        raise LauncherError(f"версия SCP:CB '{name}' не найдена")
    return vs[real]


# ------------------------------------------------------------------ инстансы


@dataclass
class Instance:
    name: str
    version: str
    exe: str
    created: str
    dir: Path

    @property
    def exe_path(self) -> Path:
        return self.dir / self.exe

    @property
    def options_ini(self) -> Path:
        return self.exe_path.parent / "options.ini"

    def save_meta(self) -> None:
        data = {"name": self.name, "version": self.version, "exe": self.exe, "created": self.created}
        (self.dir / META_FILE).write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                          encoding="utf-8")


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise LauncherError(f"недопустимое имя '{name}': латиница, цифры, '.', '-', '_'")
    return name


def list_instances(layout: Layout) -> List[Instance]:
    d = layout.scp_instances
    if not d.is_dir():
        return []
    return [_load(p) for p in sorted(d.iterdir()) if (p / META_FILE).is_file()]


def get_instance(layout: Layout, name: str | None) -> Instance:
    if name is None:
        items = list_instances(layout)
        if len(items) == 1:
            return items[0]
        if not items:
            raise LauncherError("SCP:CB не установлена. Сначала: techlauncher scp install")
        raise LauncherError("установлено несколько копий SCP:CB, укажите имя (techlauncher scp list)")
    d = layout.scp_instances / name
    if not (d / META_FILE).is_file():
        raise LauncherError(f"копия SCP:CB '{name}' не найдена (techlauncher scp list)")
    return _load(d)


def _load(d: Path) -> Instance:
    data = json.loads((d / META_FILE).read_text(encoding="utf-8"))
    return Instance(name=data["name"], version=data["version"], exe=data["exe"],
                    created=data["created"], dir=d)


def find_exe(game_dir: Path, hint: str | None = None) -> str:
    if hint:
        if (game_dir / hint).is_file():
            return Path(hint).as_posix()
        raise LauncherError(f"в папке игры нет файла {hint}")
    best = None
    for p in game_dir.rglob("*"):
        if not p.is_file() or p.suffix.lower() != ".exe":
            continue
        low = p.name.lower()
        if any(bad in low for bad in EXE_BLACKLIST):
            continue
        rel = p.relative_to(game_dir)
        score = (20 if "containment breach" in low else 0) + (10 if "scp" in low else 0)
        score -= 5 * (len(rel.parts) - 1)
        if best is None or score > best[0]:
            best = (score, rel.as_posix())
    if best is None:
        raise LauncherError(f"в {game_dir} не найден .exe игры (укажите --exe)")
    return best[1]


def _create(layout: Layout, name: str, version: str, staging: Path,
            exe_hint: str | None, force: bool) -> Instance:
    target = layout.scp_instances / name
    exe = find_exe(staging, exe_hint)
    if target.exists():
        if not force:
            shutil.rmtree(staging, ignore_errors=True)
            raise LauncherError(f"'{name}' уже установлена (переустановка: --force)")
        for item in PRESERVE_ON_REINSTALL:  # сохранения и настройки переносим в новую копию
            for src in list(target.rglob(item)):
                dst = staging / src.relative_to(target)
                if dst.is_dir():
                    shutil.rmtree(dst)
                elif dst.exists():
                    dst.unlink()
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
        shutil.rmtree(target)
    staging.rename(target)
    inst = Instance(name, version, exe, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    target)
    inst.save_meta()
    return inst


def _staging(layout: Layout, name: str) -> Path:
    layout.scp_instances.mkdir(parents=True, exist_ok=True)
    staging = layout.scp_instances / f".{name}.new"
    if staging.exists():
        shutil.rmtree(staging)
    return staging


def install(layout: Layout, version_name: str = "latest", name: str | None = None,
            force: bool = False, keep_archive: bool = False, exe: str | None = None,
            progress: net.Progress = None, log: Callable[[str], None] = print) -> Instance:
    v = resolve_version(layout, version_name)
    name = validate_name(name or v.id)
    if (layout.scp_instances / name).exists() and not force:
        raise LauncherError(f"'{name}' уже установлена (переустановка: --force)")
    log(f"Установка {v.name}" + (f" ({v.source}, {v.license})" if v.source else ""))
    archive_path = layout.cache_dir / "scpcb" / v.archive_name
    net.download_many([net.Download(v.url, archive_path, v.size, sha256=v.sha256)], progress)
    log("Распаковка...")
    staging = _staging(layout, name)
    archive.extract(archive_path, staging)
    inst = _create(layout, name, v.id, staging, exe or v.exe, force)
    if not keep_archive:
        remove_file(archive_path)
    log(f"Готово: {inst.dir}")
    return inst


def import_path(layout: Layout, src: Path, name: str, exe: str | None = None,
                label: str | None = None, force: bool = False,
                log: Callable[[str], None] = print) -> Instance:
    if not src.exists():
        raise LauncherError(f"нет такого файла или папки: {src}")
    validate_name(name)
    if (layout.scp_instances / name).exists() and not force:
        raise LauncherError(f"'{name}' уже существует (заменить: --force)")
    staging = _staging(layout, name)
    if src.is_dir():
        log(f"Копирование {src}...")
        shutil.copytree(src, staging)
    else:
        log(f"Распаковка {src.name}...")
        archive.extract(src, staging)
    inst = _create(layout, name, label or f"import:{src.name}", staging, exe, force)
    log(f"Готово: {inst.dir}")
    return inst


def remove(inst: Instance) -> None:
    shutil.rmtree(inst.dir)


# ------------------------------------------------------------------ настройки и запуск


def apply_settings(inst: Instance, settings: List[Tuple[Optional[str], str, str]],
                   warn: Callable[[str], None] = print) -> None:
    """settings: список (секция или None, ключ, значение)."""
    if not settings:
        return
    ini = IniFile(inst.options_ini)
    for section, key, value in settings:
        if not ini.set(key, value, section):
            warn(f"ключа '{key}' не было в options.ini, он добавлен")
    ini.save()


def start(layout: Layout, inst: Instance, *, resolution: Tuple[int, int] | None = None,
          fullscreen: bool | None = None, skip_launcher: bool = False,
          extra: List[Tuple[Optional[str], str, str]] | None = None,
          wine_path: str | None = None, wine_prefix: Path | None = None,
          vram: int = 512, startup_video: bool | None = None,
          args: List[str] | None = None, dry: bool = False,
          log: Callable[[str], None] = print, popen_kwargs: dict | None = None):
    """Применить настройки и запустить игру. Возвращает Popen (или команду при dry=True)."""
    if not inst.exe_path.is_file():
        raise LauncherError(f"не найден exe игры: {inst.exe_path}")
    settings: List[Tuple[Optional[str], str, str]] = []
    if resolution:
        settings += [("options", "width", str(resolution[0])), ("options", "height", str(resolution[1]))]
    if fullscreen is not None:
        settings.append(("options", "fullscreen", "true" if fullscreen else "false"))
    if skip_launcher:
        settings.append(("launcher", "launcher enabled", "false"))
    if startup_video is None:
        # Под Wine нет видеокодеков: BlitzMovie.dll портит память при попытке показать
        # вступительное видео, и игра падает на 40% загрузки с "Memory access violation".
        startup_video = not wine.needs_wine()
    if not startup_video:
        settings.append(("options", "play startup video", "false"))
    settings += extra or []
    apply_settings(inst, settings, warn=log)

    prefix = None
    if wine.needs_wine():
        prefix = wine_prefix or layout.wine_prefix
    new_prefix = prefix is not None and not prefix.exists()
    cmd, env = wine.build_command(layout, inst.exe_path, list(args or []), wine_path, prefix)
    if dry:
        return cmd
    if wine.needs_wine() and vram > 0:
        if new_prefix:
            log("Первый запуск: Wine создаёт свою папку настроек, это займёт около минуты...")
        if wine.ensure_video_memory(cmd[0], env, prefix, vram):
            log(f"Wine: игре сообщается {vram} МБ видеопамяти (защита от вылета при загрузке)")
    try:
        # Рабочая папка = папка игры: Blitz3D ищет файлы по относительным путям.
        return subprocess.Popen(cmd, cwd=str(inst.exe_path.parent), env=env, **(popen_kwargs or {}))
    except OSError as e:
        raise LauncherError(f"не удалось запустить игру: {e}") from None
