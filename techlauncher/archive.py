"""Распаковка архивов (zip, tar, а также 7z/rar через внешний 7z, если он установлен)."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

from . import LauncherError
from .paths import is_inside


def extract(archive: Path, dest: Path, trusted: bool = False) -> None:
    """Распаковать archive в папку dest (которой ещё не должно быть).

    Если внутри архива всё лежит в одной общей папке, её содержимое поднимается
    наверх, чтобы игра оказалась прямо в dest.

    trusted=True разрешает в tar-архивах символические ссылки и права на запуск как есть
    (нужно для сборок Wine, проверенных по SHA-256).
    """
    if dest.exists():
        raise LauncherError(f"папка уже существует: {dest}")

    tmp = dest.with_name(dest.name + ".extracting")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    try:
        if zipfile.is_zipfile(archive):
            _extract_zip(archive, tmp)
        elif tarfile.is_tarfile(archive):
            _extract_tar(archive, tmp, trusted)
        else:
            _extract_7z(archive, tmp)

        entries = list(tmp.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            entries[0].rename(dest)
            tmp.rmdir()
        else:
            tmp.rename(dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _safe_target(root: Path, name: str) -> Path:
    """Защита от "zip slip": файл из архива не должен попасть за пределы root."""
    target = (root / name).resolve()
    if not is_inside(target, root.resolve()):
        raise LauncherError(f"подозрительный путь в архиве: {name}")
    return target


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = _safe_target(dest, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)


def _extract_tar(archive: Path, dest: Path, trusted: bool = False) -> None:
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            _safe_target(dest, member.name)
        if hasattr(tarfile, "data_filter"):  # Python 3.12+ (и обновлённые 3.8–3.11)
            tf.extractall(dest, filter="tar" if trusted else "data")
        else:
            tf.extractall(dest)


def _extract_7z(archive: Path, dest: Path) -> None:
    exe = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
    if exe is None:
        raise LauncherError(
            f"не знаю, как распаковать {archive.name}. Для 7z/rar установите 7-Zip "
            "(Linux: пакет p7zip-full или 7zip) или распакуйте вручную и сделайте import папки"
        )
    result = subprocess.run(
        [exe, "x", "-y", f"-o{dest}", str(archive)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise LauncherError(f"7z не смог распаковать архив: {result.stderr.strip()}")


def extract_natives(jar: Path, dest: Path, exclude: list[str] | None = None) -> int:
    """Распаковать нативные библиотеки (.dll/.so) из jar старого формата Minecraft.

    exclude — префиксы путей, которые не нужны (обычно "META-INF/").
    Возвращает число распакованных файлов.
    """
    skip = tuple(exclude or ()) + ("META-INF/",)
    count = 0
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(jar) as zf:
        for info in zf.infolist():
            if info.is_dir() or info.filename.startswith(skip):
                continue
            target = _safe_target(dest, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count
