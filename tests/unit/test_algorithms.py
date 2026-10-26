# tests/unit/test_algorithms.py
"""Проверки алгоритмов на независимых ожидаемых правках и оптимальных назначениях."""

import random

import pytest

from app.domain.algorithms import (
    assignment,
    damerau_levenshtein,
    histogram,
    lcs,
    myers,
    patience,
    rolling_hash,
    smith_waterman,
)
from app.domain.text import normalize, shingles


@pytest.mark.parametrize(
    "a,b",
    [
        ("", ""),
        ("abc", ""),
        ("", "abc"),
        ("abc", "adc"),
        ("abababa", "bbabaca"),
        ("12345", "0123456"),
    ],
)
def test_myers_reconstructs_exact_sequences(a, b):
    """Вставки и удаления восстанавливают обе строки, сохраняя общие символы."""
    edits = myers(a, b)
    assert "".join(a[i] for op, i, _ in edits if op != "insert") == a
    assert "".join(b[j] for op, _, j in edits if op != "delete") == b
    assert all(a[i] == b[j] for op, i, j in edits if op == "equal")


def test_myers_is_shortest_on_random_small_inputs():
    """Сверяет длину правок с независимой динамической программой для малых строк."""
    rng = random.Random(17)
    for _ in range(100):
        a, b = "".join(rng.choices("abcd", k=12)), "".join(rng.choices("abcd", k=9))
        row = list(range(len(b) + 1))
        for i, x in enumerate(a, 1):
            new = [i]
            for j, y in enumerate(b, 1):
                new.append(row[j - 1] if x == y else min(row[j] + 1, new[-1] + 1))
            row = new
        assert sum(op != "equal" for op, _, _ in myers(a, b)) == row[-1]


def test_anchors_and_lcs_survive_insertions():
    """Устойчивые опоры не смещаются на одинаковые номера страниц."""
    assert patience(["a", "b", "c"], ["x", "a", "y", "b", "c"]) == [(0, 1), (1, 3), (2, 4)]
    assert lcs("abc", "axbc") == [(0, 0), (1, 2), (2, 3)]
    pairs = histogram(["a", "a", "b"], ["x", "a", "a", "b"])
    assert pairs == [(0, 1), (1, 2), (2, 3)]


def test_rolling_hash_and_local_alignment():
    """Перенесённое окно найдено по хешу, локальная последовательность — по SW."""
    old, new = ["alpha", "beta", "gamma"], ["x", "alpha", "beta", "gamma", "y"]
    assert rolling_hash(old, 3).keys() <= rolling_hash(new, 3).keys()
    assert smith_waterman("abcd", "xabcd") == 0.8
    assert damerau_levenshtein("ab", "ba") == 1
    assert damerau_levenshtein("0,01%", "0,1%") == 1


def test_hungarian_avoids_greedy_and_weak_matches():
    """Оптимальное назначение превосходит жадное; слабая пара остаётся без соответствия."""
    assert set(assignment([[0.9, 0.8], [0.85, 0.1]])) == {(0, 1), (1, 0)}
    assert assignment([[0.2]]) == []


def test_normalization_preserves_critical_symbols_and_mapping():
    """Карта ведёт к исходным символам, цифры/регистр/отрицание сохраняются."""
    text = "  Не\u00a0вправе\u00ad  0,01% ₽! "
    value = normalize(text)
    assert value.text == "Не вправе 0,01% ₽!"
    assert len(value.positions) == len(value.text)
    assert text[value.positions[value.text.index("0")]] == "0"
    assert shingles("4.2. Уникальная формулировка договора") == shingles(
        "7.2. Уникальная формулировка договора"
    )
