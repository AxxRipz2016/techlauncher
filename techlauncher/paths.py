"""Где лаунчер хранит данные, и мелкие помощники для работы с файлами."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "techlauncher"


def default_main_dir() -> Path:
    """Главная папка лаунчера (переопределяется TECHL_MAIN_DIR или --main-dir).

    - Windows: %APPDATA%/techlauncher
    - Linux:   $XDG_DATA_HOME/techlauncher или ~/.local/share/techlauncher
    """
    env = os.environ.get("TECHL_MAIN_DIR")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_NAME


class Layout:
    """Структура главной папки.

    main_dir/
        settings.json, accounts.json
        cache/                       временные загрузки
        java/<component>/            Java от Mojang
        wine/, wine-prefix/          переносной Wine (Linux)
        minecraft/
            versions/, libraries/, assets/, natives/
            game/                    сохранения, моды, настройки игры
        scpcb/instances/<имя>/       копии SCP:CB
    """

    def __init__(self, main_dir: Path):
        # Только абсолютный путь: игры запускаются с другой рабочей папкой,
        # и относительные пути (например --main-dir data) указывали бы не туда.
        main_dir = Path(os.path.abspath(str(Path(main_dir).expanduser())))
        self.main_dir = main_dir
        self.settings_file = main_dir / "settings.json"
        self.accounts_file = main_dir / "accounts.json"
        self.cache_dir = main_dir / "cache"
        self.java_dir = main_dir / "java"
        self.wine_dir = main_dir / "wine"
        self.wine_prefix = main_dir / "wine-prefix"

        self.mc_dir = main_dir / "minecraft"
        self.mc_versions = self.mc_dir / "versions"
        self.mc_libraries = self.mc_dir / "libraries"
        self.mc_assets = self.mc_dir / "assets"
        self.mc_natives = self.mc_dir / "natives"
        self.mc_game = self.mc_dir / "game"

        self.scp_dir = main_dir / "scpcb"
        self.scp_instances = self.scp_dir / "instances"
        self.scp_user_manifest = self.scp_dir / "versions.json"


def remove_file(path: Path) -> None:
    """Удалить файл, если он есть (path.unlink(missing_ok=True) появился только в 3.8)."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def is_inside(path: Path, root: Path) -> bool:
    """path лежит внутри root (Path.is_relative_to появился только в 3.9)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data, private: bool = False) -> None:
    """Сохранить JSON атомарно. private=True — только для владельца (токены входа)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    if private and sys.platform != "win32":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Settings:
    """Запоминает выбор пользователя между запусками (ник, версия, память...)."""

    def __init__(self, layout: Layout):
        self.path = layout.settings_file
        self.data = load_json(self.path, {}) or {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value

    def save(self) -> None:
        save_json(self.path, self.data)
