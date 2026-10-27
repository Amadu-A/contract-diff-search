# tests/unit/test_golden.py
"""Матрица проверяет конкретные значения и контекст вместо суммарного счётчика."""

from copy import deepcopy
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


def test_ai_comment_does_not_downgrade_an_independently_confirmed_fact():
    """Неподтверждённое замечание с теми же значениями учитывается отдельно от доказанного факта."""
    result = asdict(compare(version(["2.2. Аванс 70%"]), version(["2.2. Аванс 50%"])))
    comment = deepcopy(result["differences"][0])
    comment.update(id="AI-comment", source="ai", status="needs_review")
    result["differences"].append(comment)
    matrix = {
        "entries": [
            {
                "id": "seed",
                "type": "number_modified",
                "old_section": "2.2",
                "new_section": "2.2",
                "old_text": "70%",
                "new_text": "50%",
            }
        ]
    }
    report = validate_matrix(matrix, result)
    assert report["passed"] == 1 and report["unverified"] == 0
    assert "AI-comment" in report["unreviewed_difference_ids"]
