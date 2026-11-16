# tests/integration/test_advertising_contract.py
"""Приватная пара 6/4 страницы: независимые содержательные сценарии, реквизиты и экспорт."""

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict

import pytest

from app.application.queries import correspondence
from app.bootstrap import load_exclusions
from app.domain.comparison import compare
from app.infrastructure.exports import DocumentExporter
from app.infrastructure.ocr import TesseractOCR
from app.infrastructure.pdf import PdfDocumentExtractor
from app.infrastructure.word import LibreOffice
from app.settings import ROOT


def words(text):
    """Независимо от сравнивателя выделяет слова и цифры эталонной фразы."""
    return Counter(re.findall(r"\d+(?:[.,]\d+)*%?|[^\W\d_]+", text.casefold()))


def scenario_rows(matrix, result):
    """Сверяет пункт, сторону и изменённые слова каждого ручного сценария, а не число событий."""
    rows = []
    for entry in matrix["changes"]:
        checks = []
        before, after = words(entry["old_text"]), words(entry["new_text"])
        for side, expected, _other in (("old", before, after), ("new", after, before)):
            opposite = "new" if side == "old" else "old"
            opposite_clause = entry[opposite + "_clause"]
            opposite_section = "" if opposite_clause == "Шапка" else opposite_clause
            context = words(
                " ".join(
                    e.text
                    for e in getattr(result, opposite).elements
                    if e.section == opposite_section
                )
            )
            required = expected - context
            if not required:
                continue
            clause = entry[side + "_clause"]
            section = "" if clause == "Шапка" else clause
            candidates = [
                d
                for d in result.differences
                if d.active
                and d.category
                in {
                    "text_modified",
                    "number_modified",
                    "text_added",
                    "text_deleted",
                    "table_modified",
                }
                and getattr(d, side + "_section") == section
            ]
            observed = words(" ".join(getattr(d, side + "_text") for d in candidates))
            substantive = {t for t in required if len(t) >= 3 or any(c.isdigit() for c in t)}
            hits = substantive & observed.keys()
            checks.append(bool(hits) and len(hits) >= min(2, len(substantive)))
        rows.append({"id": entry["id"], "passed": bool(checks) and all(checks)})
    return rows


@pytest.mark.real
@pytest.mark.integration
def test_advertising_contract_matrix_and_all_exports(settings, tmp_path):
    """Реальные адаптеры сохраняют 41 сценарий, 13 контролей, исходники и редактируемый Word."""
    paths = [ROOT / "examples" / name for name in ("old.docx", "new.pdf", "expected_matrix.json")]
    if not all(p.is_file() for p in paths):
        pytest.skip("Приватная пара old.docx/new.pdf и её матрица отсутствуют")
    protected = [*paths, ROOT / "examples/golden-matrix.json", ROOT / "examples/matrix_review.xlsx"]
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected if p.is_file()}
    extractor = PdfDocumentExtractor(settings, TesseractOCR(settings), LibreOffice(settings))
    old, new = [
        extractor.extract(p, p.name, tmp_path / side, False, lambda _: None)
        for p, side in zip(paths[:2], ("old", "new"), strict=True)
    ]
    result = compare(
        old,
        new,
        exclusions=load_exclusions(settings),
        ocr_exclusions=load_exclusions(settings, kind="ocr"),
    )
    extractor.recheck(result, lambda _: None)
    assert (len(old.pages), len(new.pages)) == (6, 4)
    assert not any(d.category == "formatting_changed" for d in result.differences)
    matrix = json.loads(paths[2].read_text(encoding="utf8"))
    rows = scenario_rows(matrix, result)
    output = ROOT / ".cache/advertising-verified"
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(asdict(result), ensure_ascii=False), encoding="utf8"
    )
    (output / "validation.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf8"
    )
    assert all(row["passed"] for row in rows), rows
    for clause, before, after in (("2.3", "50%", "70%"), ("2.4", "50%", "30%")):
        found = [
            d for d in result.differences if d.old_section == clause or d.new_section == clause
        ]
        assert before in " ".join(d.old_text for d in found)
        assert after in " ".join(d.new_text for d in found)
    for control in matrix["unchanged_controls"]:
        contexts = {}
        for side, doc in (("old", old), ("new", new)):
            sections = control[side + "_clause"].split(" / ")
            elements = [e for e in doc.elements if e.section in sections]
            assert elements, control["id"]
            contexts[side] = " ".join(e.text for e in elements)
        if control["id"] in {"C10", "C11", "C12", "C13"}:
            left = {e.id for e in old.elements if e.section == control["old_clause"]}
            right = {e.id for e in new.elements if e.section == control["new_clause"]}
            assert any(
                left.intersection(m.old) and right.intersection(m.new) for m in result.matches
            ), control["id"]
        for number in re.findall(r"\d+(?:[.,]\d+)*%?", control["value"]):
            assert all(number in text for text in contexts.values()), (control["id"], number)
            changes = [
                d
                for d in result.differences
                if d.old_section in control["old_clause"].split(" / ")
                and d.new_section in control["new_clause"].split(" / ")
            ]
            assert not any(
                d.category == "number_modified" and d.old_text == number and d.new_text != number
                for d in changes
            ), control["id"]
        if control["id"] in {"C08", "C09"}:
            for number in re.findall(r"\d{9,}", control["value"]):
                assert not any(
                    number in d.old_text + d.new_text
                    for d in result.differences
                    if d.category in {"text_added", "text_deleted", "number_modified"}
                )
    assert not new.graphics
    assert not any("Диадок" in d.old_text + d.new_text for d in result.differences)
    deleted = next(
        d for d in result.differences if d.category == "text_deleted" and d.old_section == "8.5"
    )
    assert correspondence(asdict(result), "old", 6, deleted.id)["page"] == 4
    artifacts = DocumentExporter(LibreOffice(settings)).export(result, tmp_path, lambda _: None)
    assert all(e.path for side in artifacts.values() for e in side)
    word = next(e for e in artifacts["old"] if e.format == "docx")
    assert word.marked_changes > 0
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in hashes.items())
