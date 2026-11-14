#!/usr/bin/env python3
"""Запускалка TechLauncher с автообновлением.

Достаточно одного этого файла: при запуске он проверяет GitHub, скачивает свежую
версию лаунчера (если она вышла) и запускает её. Без интернета запускается уже
скачанная версия.

    python3 start.py                 # окно лаунчера
    python3 start.py doctor          # любые команды лаунчера
    python3 start.py --no-update ... # не проверять обновления

Скачать саму запускалку:
    https://raw.githubusercontent.com/AxxRipz2016/techlauncher/main/start.py

Только стандартная библиотека, Python 3.7+.
"""

import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = os.environ.get("TECHL_REPO", "AxxRipz2016/techlauncher")
BRANCH = os.environ.get("TECHL_BRANCH", "main")
USER_AGENT = "techlauncher-start"


def main_dir() -> Path:
    """Та же папка, что у лаунчера (TECHL_MAIN_DIR или стандартная для ОС)."""
    env = os.environ.get("TECHL_MAIN_DIR")
    if env:
        return Path(env).expanduser().absolute()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / "techlauncher"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "techlauncher"


def say(msg: str) -> None:
    print(f"[start] {msg}", flush=True)


def latest_commit() -> str:
    url = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))["sha"]


def download(sha: str, app: Path) -> None:
    """Скачать архив коммита и аккуратно подменить им текущую версию."""
    url = f"https://codeload.github.com/{REPO}/zip/{sha}"
    tmp_zip = app / f"download-{sha[:12]}.zip"
    new_dir = app / f"new-{sha[:12]}"
    app.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp_zip, "wb") as out:
        shutil.copyfileobj(resp, out)

    if new_dir.exists():
        shutil.rmtree(new_dir)
    new_dir.mkdir()
    root = new_dir.resolve()
    with zipfile.ZipFile(tmp_zip) as zf:
        for info in zf.infolist():
            # в архиве всё лежит в папке "techlauncher-<sha>/", её убираем
            parts = info.filename.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            target = (new_dir / parts[1]).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"подозрительный путь в архиве: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    tmp_zip.unlink()

    if not (new_dir / "techlauncher" / "__init__.py").is_file():
        shutil.rmtree(new_dir)
        raise RuntimeError("в скачанном архиве нет лаунчера")
    (new_dir / "COMMIT").write_text(sha, encoding="utf-8")

    current, old = app / "current", app / "old"
    if old.exists():
        shutil.rmtree(old)
    if current.exists():
        current.rename(old)
    new_dir.rename(current)
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)


def update(app: Path) -> None:
    current = app / "current"
    have = (current / "COMMIT").read_text(encoding="utf-8").strip() \
        if (current / "COMMIT").is_file() else None
    try:
        sha = latest_commit()
    except (OSError, ValueError, KeyError) as e:
        if have:
            say(f"нет связи с GitHub ({e}), запускаю скачанную версию {have[:7]}")
            return
        raise SystemExit(f"[start] не удалось связаться с GitHub ({e}), а лаунчер ещё не скачан.\n"
                         f"Скачайте вручную: https://github.com/{REPO}")
    if sha == have:
        return
    say(("обновление " + have[:7] + " -> " if have else "скачивание лаунчера, версия ") + sha[:7])
    try:
        download(sha, app)
    except (OSError, RuntimeError, zipfile.BadZipFile) as e:
        if have:
            say(f"не удалось обновиться ({e}), запускаю версию {have[:7]}")
            return
        raise SystemExit(f"[start] не удалось скачать лаунчер: {e}")


def main() -> int:
    if sys.version_info < (3, 7):
        print("Нужен Python 3.7 или новее")
        return 1
    args = sys.argv[1:]
    no_update = "--no-update" in args or os.environ.get("TECHL_NO_UPDATE") == "1"
    args = [a for a in args if a != "--no-update"]

    app = main_dir() / "app"
    if not no_update:
        update(app)
    current = app / "current"
    if not (current / "techlauncher" / "__init__.py").is_file():
        print(f"[start] лаунчер не найден в {current}; запустите без --no-update")
        return 1

    sys.path.insert(0, str(current))
    from techlauncher.cli import main as launcher_main
    return launcher_main(args)


if __name__ == "__main__":
    sys.exit(main())
