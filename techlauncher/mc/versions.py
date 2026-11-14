"""Манифест версий Mojang и JSON-файлы версий с наследованием (inheritsFrom)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from .. import LauncherError, net
from ..paths import Layout, save_json

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


def manifest(layout: Layout, refresh: bool = True) -> Optional[dict]:
    """Манифест версий. Без интернета используется сохранённая копия (или None)."""
    cache = layout.mc_dir / "version_manifest_v2.json"
    if refresh:
        try:
            data = net.get_json(MANIFEST_URL)
            save_json(cache, data)
            return data
        except LauncherError:
            pass
    if cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


def resolve(man: Optional[dict], name: str | None) -> str:
    """'release'/'snapshot'/'latest' -> конкретный id версии."""
    name = (name or "release").strip()
    if name in ("release", "latest", "snapshot"):
        if man is None:
            raise LauncherError("нет связи с Mojang, а последняя версия ещё неизвестна. "
                                "Укажите конкретную установленную версию")
        return man["latest"]["snapshot" if name == "snapshot" else "release"]
    return name


def entry(man: Optional[dict], version_id: str) -> Optional[dict]:
    if not man:
        return None
    return next((v for v in man.get("versions", []) if v["id"] == version_id), None)


def installed(layout: Layout) -> List[str]:
    if not layout.mc_versions.is_dir():
        return []
    return sorted(p.name for p in layout.mc_versions.iterdir()
                  if (p / f"{p.name}.json").is_file())


def version_json_path(layout: Layout, version_id: str) -> Path:
    return layout.mc_versions / version_id / f"{version_id}.json"


def load(layout: Layout, version_id: str, man: Optional[dict]) -> dict:
    """JSON одной версии; скачивается, если его нет или он не совпадает с манифестом."""
    path = version_json_path(layout, version_id)
    e = entry(man, version_id)
    if e is not None and not net.file_ok(path, None, e.get("sha1"), deep=True):
        net.download_file(net.Download(e["url"], path, sha1=e.get("sha1")))
    if not path.is_file():
        hint = "" if man else " (и нет связи с Mojang)"
        raise LauncherError(f"версия Minecraft '{version_id}' не найдена{hint}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise LauncherError(f"повреждён файл {path}: {e}") from None
    data["_id"] = version_id
    return data


def hierarchy(layout: Layout, version_id: str, man: Optional[dict]) -> List[dict]:
    """[версия, её родитель, родитель родителя, ...] — например fabric -> vanilla."""
    chain: List[dict] = []
    seen = set()
    current: Optional[str] = version_id
    while current:
        if current in seen:
            raise LauncherError(f"циклическое наследование версий: {current}")
        seen.add(current)
        data = load(layout, current, man)
        chain.append(data)
        current = data.get("inheritsFrom")
    return chain


def merge(chain: List[dict]) -> dict:
    """Собрать итоговое описание версии из цепочки наследования."""
    merged: Dict = {"id": chain[0]["_id"], "libraries": [],
                    "arguments": {"game": [], "jvm": []}, "modern_arguments": False}
    for key in ("mainClass", "assetIndex", "assets", "javaVersion", "logging", "type",
                "minecraftArguments"):
        for v in chain:
            if key in v:
                merged[key] = v[key]
                break
    for v in chain:  # client.jar берётся у первой версии, у которой он есть
        client = (v.get("downloads") or {}).get("client")
        if client:
            merged["client"] = client
            merged["client_id"] = v["_id"]
            break
    for v in reversed(chain):  # сначала аргументы родителя, потом дочерней версии
        args = v.get("arguments")
        if args:
            merged["modern_arguments"] = True
            merged["arguments"]["game"] += args.get("game", [])
            merged["arguments"]["jvm"] += args.get("jvm", [])
    for v in chain:  # библиотеки дочерней версии важнее (дубликаты отсеет install)
        merged["libraries"] += v.get("libraries", [])
    if "mainClass" not in merged:
        raise LauncherError(f"у версии {merged['id']} не указан mainClass")
    return merged
