"""Что нужно скачать и подготовить для версии Minecraft."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import LauncherError, archive, net
from ..paths import Layout
from . import rules

LIBRARIES_URL = "https://libraries.minecraft.net/"
RESOURCES_URL = "https://resources.download.minecraft.net/"


def maven_path(name: str) -> str:
    """'group:artifact:version[:classifier][@ext]' -> путь внутри libraries/.

    Пример: 'org.lwjgl:lwjgl:3.3.3:natives-linux'
         -> 'org/lwjgl/lwjgl/3.3.3/lwjgl-3.3.3-natives-linux.jar'
    """
    ext = "jar"
    if "@" in name:
        name, ext = name.split("@", 1)
    parts = name.split(":")
    if len(parts) < 3:
        raise LauncherError(f"странное имя библиотеки: {name}")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 else None
    filename = f"{artifact}-{version}" + (f"-{classifier}" if classifier else "") + f".{ext}"
    return "/".join(group.split(".") + [artifact, version, filename])


def library_key(name: str) -> str:
    """Ключ для поиска дубликатов: группа, артефакт и classifier без версии."""
    parts = name.split("@", 1)[0].split(":")
    return ":".join(parts[:2] + parts[3:])


@dataclass
class Plan:
    downloads: List[net.Download] = field(default_factory=list)
    classpath: List[Path] = field(default_factory=list)
    natives: List[Tuple[Path, List[str]]] = field(default_factory=list)  # (jar, exclude)
    asset_index_id: Optional[str] = None
    asset_index: Optional[dict] = None
    log_argument: Optional[str] = None
    log_file: Optional[Path] = None


def _add_library(layout: Layout, plan: Plan, lib: dict, seen: set) -> None:
    name = lib["name"]
    downloads = lib.get("downloads") or {}

    natives_map = lib.get("natives")
    if natives_map:  # старый формат: нативные файлы в отдельном jar, их распаковывают
        classifier = natives_map.get(rules.os_name())
        if classifier:
            classifier = classifier.replace("${arch}", rules.os_bits())
            key = library_key(f"{name}:{classifier}")
            if key not in seen:
                seen.add(key)
                art = (downloads.get("classifiers") or {}).get(classifier)
                if art:
                    path = layout.mc_libraries / (art.get("path") or maven_path(f"{name}:{classifier}"))
                    plan.downloads.append(net.Download(art["url"], path, art.get("size"), art.get("sha1")))
                else:
                    rel = maven_path(f"{name}:{classifier}")
                    path = layout.mc_libraries / rel
                    base = (lib.get("url") or LIBRARIES_URL).rstrip("/") + "/"
                    plan.downloads.append(net.Download(base + rel, path))
                plan.natives.append((path, (lib.get("extract") or {}).get("exclude", [])))
        if "artifact" not in downloads:
            return

    key = library_key(name)
    if key in seen:
        return
    seen.add(key)

    art = downloads.get("artifact")
    if art is not None:
        path = layout.mc_libraries / (art.get("path") or maven_path(name))
        if art.get("url"):  # пустой url — файл создаётся установщиком модлоадера
            plan.downloads.append(net.Download(art["url"], path, art.get("size"), art.get("sha1")))
    else:  # формат Fabric/Quilt: только имя и адрес maven-репозитория
        rel = maven_path(name)
        path = layout.mc_libraries / rel
        base = (lib.get("url") or LIBRARIES_URL).rstrip("/") + "/"
        plan.downloads.append(net.Download(base + rel, path, lib.get("size"), lib.get("sha1")))
    plan.classpath.append(path)


def plan_files(layout: Layout, merged: dict, features: Dict[str, bool]) -> Plan:
    plan = Plan()

    seen: set = set()
    for lib in merged["libraries"]:
        if rules.allowed(lib.get("rules"), features):
            _add_library(layout, plan, lib, seen)

    # client.jar идёт последним в classpath, как у официального лаунчера
    client = merged.get("client")
    client_id = merged.get("client_id", merged["id"])
    jar = layout.mc_versions / client_id / f"{client_id}.jar"
    if client:
        plan.downloads.append(net.Download(client["url"], jar, client.get("size"), client.get("sha1")))
    elif not jar.is_file():
        raise LauncherError(f"у версии {merged['id']} нет client.jar")
    plan.classpath.append(jar)

    # индекс ассетов нужен сразу, чтобы узнать список файлов
    asset_index = merged.get("assetIndex")
    if asset_index:
        plan.asset_index_id = asset_index["id"]
        index_path = layout.mc_assets / "indexes" / f"{asset_index['id']}.json"
        net.download_file(net.Download(asset_index["url"], index_path,
                                       asset_index.get("size"), asset_index.get("sha1")))
        plan.asset_index = json.loads(index_path.read_text(encoding="utf-8"))
        for obj in plan.asset_index.get("objects", {}).values():
            h = obj["hash"]
            plan.downloads.append(net.Download(
                f"{RESOURCES_URL}{h[:2]}/{h}", layout.mc_assets / "objects" / h[:2] / h,
                obj.get("size"), h))

    logging = (merged.get("logging") or {}).get("client")
    if logging and logging.get("file"):
        f = logging["file"]
        plan.log_file = layout.mc_assets / "log_configs" / f["id"]
        plan.log_argument = logging.get("argument")
        plan.downloads.append(net.Download(f["url"], plan.log_file, f.get("size"), f.get("sha1")))
    return plan


def finalize(layout: Layout, version_id: str, plan: Plan) -> Tuple[Path, Optional[Path]]:
    """После загрузки: распаковать нативные файлы и подготовить старые ассеты.

    Возвращает (папка нативных файлов, папка "виртуальных" ассетов или None).
    """
    natives_dir = layout.mc_natives / version_id
    if natives_dir.exists():
        shutil.rmtree(natives_dir)
    natives_dir.mkdir(parents=True)
    for jar, exclude in plan.natives:
        archive.extract_natives(jar, natives_dir, exclude)

    virtual_dir = None
    index = plan.asset_index or {}
    targets = []
    if index.get("virtual"):  # версии до 1.7: ассеты лежат под своими именами
        virtual_dir = layout.mc_assets / "virtual" / (plan.asset_index_id or "legacy")
        targets.append(virtual_dir)
    if index.get("map_to_resources"):  # совсем старые версии: папка resources в игре
        targets.append(layout.mc_game / "resources")
    for target in targets:
        for name, obj in index.get("objects", {}).items():
            h = obj["hash"]
            dst = target / name
            if dst.is_file() and dst.stat().st_size == obj.get("size"):
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(layout.mc_assets / "objects" / h[:2] / h, dst)
    return natives_dir, virtual_dir
