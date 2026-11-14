"""Подготовка версии и запуск Minecraft."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .. import LauncherError, __version__, net
from ..paths import Layout
from . import install, java, rules, versions
from .auth import Account

# Аргументы старых версий, у которых в JSON есть только "minecraftArguments".
LEGACY_JVM_ARGS = ["-Djava.library.path=${natives_directory}", "-cp", "${classpath}"]

# progress(этап, скачано_байт, всего_байт, скачано_файлов, всего_файлов)
StageProgress = Optional[Callable[[str, int, int, int, int], None]]


@dataclass
class Options:
    memory_mb: int = 2048
    resolution: Optional[Tuple[int, int]] = None
    java_path: Optional[str] = None
    demo: bool = False
    server: Optional[str] = None          # "host" или "host:port"
    extra_jvm: List[str] = field(default_factory=list)


@dataclass
class Prepared:
    version_id: str
    merged: dict
    plan: install.Plan
    natives_dir: Path
    virtual_assets: Optional[Path]
    java: Path


def _features(opts: Options) -> Dict[str, bool]:
    return {
        "is_demo_user": opts.demo,
        "has_custom_resolution": opts.resolution is not None,
        "has_quick_play_support": False,
        "is_quick_play_singleplayer": False,
        "is_quick_play_multiplayer": False,
        "is_quick_play_realms": False,
    }


def prepare(layout: Layout, version: str, opts: Options, progress: StageProgress = None,
            log: Callable[[str], None] = print) -> Prepared:
    """Скачать всё нужное для версии. Повторный вызов проверяет файлы и докачивает недостающие."""
    man = versions.manifest(layout)
    version_id = versions.resolve(man, version)
    log(f"Minecraft {version_id}: проверка файлов...")
    chain = versions.hierarchy(layout, version_id, man)
    merged = versions.merge(chain)

    def stage(name: str) -> net.Progress:
        if progress is None:
            return None
        return lambda b, tb, f, tf: progress(name, b, tb, f, tf)

    if opts.java_path:
        java_path = Path(opts.java_path)
        if not java_path.is_file():
            raise LauncherError(f"нет файла Java: {java_path}")
    else:
        java_path = java.ensure(layout, java.component_for(merged), stage("Java"), log)

    plan = install.plan_files(layout, merged, _features(opts))
    count = net.download_many(plan.downloads, stage("Файлы игры"))
    if count:
        log(f"Скачано файлов: {count}")
    natives_dir, virtual = install.finalize(layout, version_id, plan)
    return Prepared(version_id, merged, plan, natives_dir, virtual, java_path)


def _expand(items: list, features: Dict[str, bool]) -> List[str]:
    out: List[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif rules.allowed(item.get("rules"), features):
            value = item.get("value")
            out.extend(value if isinstance(value, list) else [value])
    return out


_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def _substitute(args: List[str], values: Dict[str, str]) -> List[str]:
    return [_PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), a) for a in args]


def command(layout: Layout, prep: Prepared, account: Account, opts: Options) -> List[str]:
    merged = prep.merged
    features = _features(opts)

    server_host, server_port = None, "25565"
    if opts.server:
        server_host, _, port = opts.server.partition(":")
        server_port = port or "25565"
        quick_play = "is_quick_play_multiplayer" in json.dumps(merged["arguments"]["game"])
        features["is_quick_play_multiplayer"] = quick_play

    if merged["modern_arguments"]:
        jvm_args = _expand(merged["arguments"]["jvm"], features)
        game_args = _expand(merged["arguments"]["game"], features)
    else:
        jvm_args = list(LEGACY_JVM_ARGS)
        game_args = (merged.get("minecraftArguments") or "").split()
        if opts.resolution:
            game_args += ["--width", "${resolution_width}", "--height", "${resolution_height}"]
    if opts.server and not features["is_quick_play_multiplayer"]:
        game_args += ["--server", server_host, "--port", server_port]

    layout.mc_game.mkdir(parents=True, exist_ok=True)
    width, height = opts.resolution or (854, 480)
    values = {
        "auth_player_name": account.username,
        "auth_uuid": account.uuid,
        "auth_access_token": account.access_token,
        "auth_session": f"token:{account.access_token}:{account.uuid}",
        "auth_xuid": account.xuid or "0",
        "clientid": "",
        "user_type": account.user_type,
        "user_properties": "{}",
        "version_name": prep.version_id,
        "version_type": merged.get("type", "release"),
        "game_directory": str(layout.mc_game),
        "assets_root": str(layout.mc_assets),
        "game_assets": str(prep.virtual_assets or layout.mc_assets),
        "assets_index_name": prep.plan.asset_index_id or "legacy",
        "natives_directory": str(prep.natives_dir),
        "library_directory": str(layout.mc_libraries),
        "classpath": os.pathsep.join(str(p) for p in prep.plan.classpath),
        "classpath_separator": os.pathsep,
        "launcher_name": "techlauncher",
        "launcher_version": __version__,
        "resolution_width": str(width),
        "resolution_height": str(height),
        "quickPlayMultiplayer": f"{server_host}:{server_port}" if server_host else "",
        "quickPlayPath": "",
    }

    cmd = [str(prep.java), f"-Xmx{opts.memory_mb}M", f"-Xms{min(opts.memory_mb, 512)}M"]
    cmd += _substitute(jvm_args, values)
    if prep.plan.log_argument and prep.plan.log_file:
        cmd.append(prep.plan.log_argument.replace("${path}", str(prep.plan.log_file)))
    cmd += opts.extra_jvm
    cmd.append(merged["mainClass"])
    cmd += _substitute(game_args, values)
    return cmd


def start(layout: Layout, prep: Prepared, account: Account, opts: Options,
          popen_kwargs: Optional[dict] = None) -> subprocess.Popen:
    cmd = command(layout, prep, account, opts)
    try:
        return subprocess.Popen(cmd, cwd=str(layout.mc_game), **(popen_kwargs or {}))
    except OSError as e:
        raise LauncherError(f"не удалось запустить Java: {e}") from None


class LogFormatter:
    """Переводит XML-лог игры (формат log4j от Mojang) в обычные строки.

    Пример входа:
        <log4j:Event logger="..." timestamp="1794663325544" level="INFO" thread="Render thread">
          <log4j:Message><![CDATA[Setting user: Danik]]></log4j:Message>
        </log4j:Event>
    Выход: "[12:34:56] [Render thread/INFO] Setting user: Danik"
    Строки не в формате XML передаются как есть.
    """

    _EVENT = re.compile(r'<log4j:Event\b[^>]*?timestamp="(\d+)"[^>]*?level="(\w+)"[^>]*?thread="([^"]*)"')

    def __init__(self) -> None:
        self.header: Optional[str] = None
        self.text: List[str] = []
        self.in_cdata = False

    def feed(self, line: str) -> List[str]:
        line = line.rstrip("\r\n")
        if self.header is None:
            m = self._EVENT.search(line)
            if not m:
                return [line] if line.strip() else []
            import time as _time
            stamp = _time.strftime("%H:%M:%S", _time.localtime(int(m.group(1)) / 1000))
            self.header = f"[{stamp}] [{m.group(3)}/{m.group(2)}]"
            self.text = []
            return []
        if "</log4j:Event>" in line:
            out = [f"{self.header} {self.text[0] if self.text else ''}"] + self.text[1:]
            self.header = None
            return out
        if self.in_cdata or "<![CDATA[" in line:
            part = line.split("<![CDATA[", 1)[1] if "<![CDATA[" in line else line
            if "]]>" in part:
                part = part.split("]]>", 1)[0]
                self.in_cdata = False
            else:
                self.in_cdata = True
            self.text.append(part)
        return []


def mask_command(cmd: List[str], account: Account) -> List[str]:
    """Команда для показа на экране: без токена доступа."""
    secret = account.access_token
    if not secret or secret == "0":
        return cmd
    return [c.replace(secret, "***") for c in cmd]
