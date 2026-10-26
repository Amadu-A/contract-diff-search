# tests/unit/test_golden.py
"""Матрица проверяет конкретные значения и контекст вместо суммарного счётчика."""

from dataclasses import asdict

from app.application.golden import validate_matrix
from app.domain.comparison import compare
from tests.factories import version


def test_matrix_rejects_right_number_in_wrong_clause():
    """Число из другого пункта не удовлетворяет эталонному изменению аванса."""
    result = asdict(compare(version(["4.3.1. Платёж 70%"]), version(["4.3.1. Платёж 50%"])))
    matrix = {
        "exhaustive": False,
        "entries": [
            {
                "id": "seed",
                "type": "number_modified",
                "old_section": "2.2",
                "new_section": "2.2",
                "old_text": "70%",
                "new_text": "50%",
            }
        ],
    }
    report = validate_matrix(matrix, result)
    assert report["failed"] == 1
    assert report["precision"] is None
