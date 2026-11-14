"""Редактирование options.ini без потери форматирования и комментариев.

Стандартный configparser переписал бы файл целиком (и сменил регистр ключей),
поэтому здесь простой построчный редактор. Файл читается как latin-1: эта
кодировка без потерь переводит любые байты в текст и обратно.
"""

from __future__ import annotations

from pathlib import Path

ENCODING = "latin-1"


class IniFile:
    def __init__(self, path: Path):
        self.path = path
        self.newline = "\r\n"
        self.lines: list[str] = []
        if path.is_file():
            raw = path.read_bytes().decode(ENCODING)
            if "\r\n" not in raw and "\n" in raw:
                self.newline = "\n"
            self.lines = raw.splitlines()

    @staticmethod
    def _section_of(line: str) -> str | None:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            return s[1:-1].strip()
        return None

    @staticmethod
    def _split(line: str) -> tuple[str, str] | None:
        s = line.strip()
        if not s or s.startswith(";") or s.startswith("#") or "=" not in s:
            return None
        key, value = s.split("=", 1)
        return key.strip(), value.strip()

    def items(self) -> list[tuple[str, str, str]]:
        """Список (секция, ключ, значение)."""
        out = []
        section = ""
        for line in self.lines:
            sec = self._section_of(line)
            if sec is not None:
                section = sec
                continue
            kv = self._split(line)
            if kv:
                out.append((section, kv[0], kv[1]))
        return out

    def _find(self, key: str, section: str | None) -> int | None:
        cur = ""
        for i, line in enumerate(self.lines):
            sec = self._section_of(line)
            if sec is not None:
                cur = sec
                continue
            kv = self._split(line)
            if kv and kv[0].lower() == key.lower():
                if section is None or cur.lower() == section.lower():
                    return i
        return None

    def get(self, key: str, section: str | None = None) -> str | None:
        i = self._find(key, section)
        return None if i is None else self._split(self.lines[i])[1]  # type: ignore[index]

    def set(self, key: str, value: str, section: str | None = None,
            default_section: str = "options") -> bool:
        """Задать значение. Возвращает True, если ключ уже был в файле.

        Если ключа нет, он добавляется в конец секции section (или default_section).
        """
        i = self._find(key, section)
        if i is not None:
            line = self.lines[i]
            eq = line.index("=")
            spaced = line[eq + 1:eq + 2] == " "
            self.lines[i] = line[:eq + 1] + (" " if spaced else "") + value
            return True

        target = section or default_section
        insert_at = None
        cur = None
        for idx, line in enumerate(self.lines):
            sec = self._section_of(line)
            if sec is not None:
                if cur is not None and cur.lower() == target.lower():
                    break
                cur = sec
            if cur is not None and cur.lower() == target.lower() and line.strip():
                insert_at = idx + 1
        if insert_at is None:
            if self.lines and self.lines[-1].strip():
                self.lines.append("")
            self.lines.append(f"[{target}]")
            insert_at = len(self.lines)
        self.lines.insert(insert_at, f"{key} = {value}")
        return False

    def save(self) -> None:
        text = self.newline.join(self.lines) + self.newline
        self.path.write_bytes(text.encode(ENCODING, errors="replace"))
