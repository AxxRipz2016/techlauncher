"""Правила из JSON версий Mojang: что разрешено на этой ОС и при этих "features".

Пример правила:
    {"action": "allow", "os": {"name": "linux"}}
    {"action": "allow", "features": {"has_custom_resolution": true}}

Если правил нет — разрешено. Если есть — по умолчанию запрещено, и каждое
подходящее правило по очереди задаёт итог (allow/disallow).
"""

from __future__ import annotations

import platform
import re
import sys
from typing import Dict, List, Optional


def os_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "osx"
    return "linux"


def os_arch() -> str:
    machine = platform.machine().lower()
    return {
        "amd64": "x86_64", "x86_64": "x86_64",
        "i386": "x86", "i686": "x86", "x86": "x86",
        "aarch64": "arm64", "arm64": "arm64",
    }.get(machine, machine)


def os_bits() -> str:
    return "64" if sys.maxsize > 2**32 else "32"


def _os_version() -> str:
    return platform.version() if os_name() == "windows" else platform.release()


def os_matches(rule_os: Dict) -> bool:
    name = rule_os.get("name")
    if name and name != os_name():
        return False
    arch = rule_os.get("arch")
    if arch and arch != os_arch():
        return False
    version = rule_os.get("version")
    if version:
        try:
            if not re.search(version, _os_version()):
                return False
        except re.error:
            return False
    return True


def allowed(rules: Optional[List[Dict]], features: Optional[Dict[str, bool]] = None) -> bool:
    if not rules:
        return True
    features = features or {}
    result = False
    for rule in rules:
        if "os" in rule and not os_matches(rule["os"]):
            continue
        wanted = rule.get("features") or {}
        if any(bool(features.get(k)) != bool(v) for k, v in wanted.items()):
            continue
        result = rule.get("action") == "allow"
    return result
