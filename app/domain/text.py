# app/domain/text.py
"""Нормализация для поиска с обратной картой, без потери чисел и пунктуации."""

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

EquivalenceGroups = tuple[tuple[str, ...], ...]

TOKEN = re.compile(r"\d+(?:[.,]\d+)*(?:\s?[%₽€$])?|[^\W\d_]+|[^\w\s]", re.UNICODE)
SECTION = re.compile(r"^(?=\d{1,3}\.)(\d{1,3}(?:\.\d{1,3}){0,6})\.?\s+")
NUMBER = SECTION


@dataclass(frozen=True)
class NormalizedText:
    """Строка поиска и позиции её символов в исходном представлении."""

    text: str
    positions: tuple[int, ...]
    ends: tuple[int, ...] = ()


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


@lru_cache(maxsize=32)
def _equivalence_pattern(groups: EquivalenceGroups) -> tuple[re.Pattern, dict[str, str]]:
    """Компилирует независимые буквальные замены; длинное значение имеет приоритет."""
    replacements = {value: group[0] for group in groups for value in group}
    pattern = re.compile(
        "|".join(re.escape(v) for v in sorted(replacements, key=len, reverse=True))
    )
    return pattern, replacements


def canonicalize(text: str, groups: EquivalenceGroups = ()) -> NormalizedText:
    """Применяет группы только к поисковой копии, сохраняя начала и концы исходных диапазонов."""
    normalized = normalize(text)
    if not groups:
        return normalized
    pattern, replacements = _equivalence_pattern(tuple(tuple(group) for group in groups))
    chars, positions, ends = [], [], []
    cursor = 0
    for match in pattern.finditer(normalized.text):
        chars.append(normalized.text[cursor : match.start()])
        positions.extend(normalized.positions[cursor : match.start()])
        ends.extend(p + 1 for p in normalized.positions[cursor : match.start()])
        replacement = replacements[match.group()]
        chars.append(replacement)
        positions.extend([normalized.positions[match.start()]] * len(replacement))
        ends.extend([normalized.positions[match.end() - 1] + 1] * len(replacement))
        cursor = match.end()
    chars.append(normalized.text[cursor:])
    positions.extend(normalized.positions[cursor:])
    ends.extend(p + 1 for p in normalized.positions[cursor:])
    return NormalizedText("".join(chars), tuple(positions), tuple(ends))


def comparison_tokens(text: str, groups: EquivalenceGroups = ()) -> list[tuple[str, int, int]]:
    """Сравнивает канонические токены независимо от OCR-разбиения, возвращая исходные диапазоны."""
    mapped = canonicalize(text, groups)
    return [
        (
            value.replace(" ", "") if value[0].isdigit() else value,
            mapped.positions[start],
            mapped.ends[end - 1] if mapped.ends else mapped.positions[end - 1] + 1,
        )
        for value, start, end in tokens(mapped.text)
    ]


def comparison_content(text: str, groups: EquivalenceGroups = ()) -> str:
    """Даёт ключ содержимого для сопоставления, игнорируя только настроенные замены и OCR-пробелы."""
    value = content(text)
    return " ".join(t[0] for t in comparison_tokens(value, groups)) if groups else value


def exclusion_value(text: str, groups: EquivalenceGroups) -> bool:
    """Распознаёт целое значение группы для структурного сопоставления и оценки OCR знаков."""
    return any(text.strip() in group for group in groups)


def excluded_pair(old: str, new: str, a, b, groups: EquivalenceGroups) -> bool:
    """Проверяет эквивалентность соответствующих диапазонов без удаления окружающего содержания."""
    if not a or not b or not groups:
        return False
    before, after = old[slice(*a)], new[slice(*b)]
    return (
        bool(before.strip() and after.strip())
        and any(value in before or value in after for group in groups for value in group)
        and [t[0] for t in comparison_tokens(before, groups)]
        == [t[0] for t in comparison_tokens(after, groups)]
    )


def shingles(text: str, size: int = 3) -> set[tuple[str, ...]]:
    """Строит словесные n-граммы для ограниченного поиска частичных соответствий."""
    words = tuple(t[0].casefold() for t in tokens(content(text)))
    return {words[i : i + size] for i in range(max(1, len(words) - size + 1))}


def similarity(old: str, new: str) -> float:
    """Оценивает кандидата по Жаккару; оценка не подтверждает равенство содержания."""
    a, b = shingles(old), shingles(new)
    return len(a & b) / max(1, len(a | b))
