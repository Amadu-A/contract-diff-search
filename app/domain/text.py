# app/domain/text.py
"""Нормализация для поиска с обратной картой, без потери чисел и пунктуации."""

import re
import unicodedata
from dataclasses import dataclass

TOKEN = re.compile(r"\d+(?:[.,]\d+)*(?:\s?[%₽€$])?|[^\W\d_]+|[^\w\s]", re.UNICODE)
SECTION = re.compile(r"^(?=\d{1,3}\.)(\d{1,3}(?:\.\d{1,3}){0,6})\.?\s+")
NUMBER = SECTION


@dataclass(frozen=True)
class NormalizedText:
    """Строка поиска и позиции её символов в исходном представлении."""

    text: str
    positions: tuple[int, ...]


def normalize(text: str) -> NormalizedText:
    """Убирает технические пробелы и мягкие переносы, сохраняя регистр и знаки."""
    chars, positions = [], []
    for i, char in enumerate(text):
        if char == "\u00ad":
            continue
        for value in unicodedata.normalize("NFC", char):
            if value.isspace():
                if chars and chars[-1] != " ":
                    chars.append(" ")
                    positions.append(i)
            else:
                chars.append(value)
                positions.append(i)
    if chars and chars[-1] == " ":
        chars.pop()
        positions.pop()
    return NormalizedText("".join(chars), tuple(positions))


def tokens(text: str) -> list[tuple[str, int, int]]:
    """Разбивает текст на числовые, словесные и пунктуационные токены с диапазонами."""
    return [(m.group(), m.start(), m.end()) for m in TOKEN.finditer(text)]


def content(text: str) -> str:
    """Убирает только начальную нумерацию для поиска перенумерованных пунктов."""
    return NUMBER.sub("", normalize(text).text)


def exclusion_value(text: str, values) -> bool:
    """Не позволяет расширением списка отключить проверку чисел, дат и дефисов внутри слов."""
    value = text.strip()
    return value in values and not re.search(r"\d|(?<=\w)[\-–—−]+(?=\w)", value)


def excluded_pair(old: str, new: str, a, b, values) -> bool:
    """Разрешает целые значения в сопоставленном диапазоне, сохраняя дефисы слов, числа и даты."""
    if (
        not a
        or not b
        or not exclusion_value(old[slice(*a)], values)
        or not exclusion_value(new[slice(*b)], values)
    ):
        return False
    for text, (start, end) in ((old, a), (new, b)):
        # Прилегающие буквы/цифры означают составное слово, знак числа или дату.
        if start and text[start - 1].isalnum() or end < len(text) and text[end].isalnum():
            return False
        left, right = text[:start].rstrip(), text[end:].lstrip()
        if left and left[-1].isdigit() or right and right[0].isdigit():
            return False
    return True


def shingles(text: str, size: int = 3) -> set[tuple[str, ...]]:
    """Строит словесные n-граммы для ограниченного поиска частичных соответствий."""
    words = tuple(t[0].casefold() for t in tokens(content(text)))
    return {words[i : i + size] for i in range(max(1, len(words) - size + 1))}


def similarity(old: str, new: str) -> float:
    """Оценивает кандидата по Жаккару; оценка не подтверждает равенство содержания."""
    a, b = shingles(old), shingles(new)
    return len(a & b) / max(1, len(a | b))
