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


def shingles(text: str, size: int = 3) -> set[tuple[str, ...]]:
    """Строит словесные n-граммы для ограниченного поиска частичных соответствий."""
    words = tuple(t[0].casefold() for t in tokens(content(text)))
    return {words[i : i + size] for i in range(max(1, len(words) - size + 1))}


def similarity(old: str, new: str) -> float:
    """Оценивает кандидата по Жаккару; оценка не подтверждает равенство содержания."""
    a, b = shingles(old), shingles(new)
    return len(a & b) / max(1, len(a | b))
