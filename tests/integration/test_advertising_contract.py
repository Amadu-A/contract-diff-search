# tests/integration/test_advertising_contract.py
"""Приватная пара 6/4 страницы: независимые содержательные сценарии, реквизиты и экспорт."""

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import pytest

from app.application.queries import comparison_report, correspondence
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


def assert_unchanged_fragment(result, control):
    """Проверяет реальные диапазоны общего текста, а не только его присутствие в редакциях."""
    pattern = re.compile(r"\s+".join(re.escape(part) for part in control["value"].split()))
    located = {}
    for side in ("old", "new"):
        found = [
            (e, m)
            for e in getattr(result, side).elements
            if e.section == control[side + "_clause"]
            for m in pattern.finditer(e.text)
        ]
        assert len(found) == 1, (control["id"], side)
        element, span = found[0]
        assert any(
            w.region.page == control[side + "_page"]
            for w in element.words
            if w.end > span.start() and w.start < span.end()
        )
        located[side] = element.id
        for difference in result.differences:
            if not difference.active or getattr(difference, side + "_element") != element.id:
                continue
            if difference.category not in {
                "text_added",
                "text_deleted",
                "text_modified",
                "number_modified",
                "text_moved",
            }:
                continue
            changed = getattr(difference, side + "_range")
            assert changed and (changed[1] <= span.start() or changed[0] >= span.end()), (
                control["id"],
                difference,
            )
    mapped = [m for m in result.matches if located["old"] in m.old and located["new"] in m.new]
    assert mapped, control["id"]
    if control["kind"] == "split_merge":
        assert any(len(m.old) > 1 or len(m.new) > 1 for m in mapped)
    return pattern


@pytest.mark.real
@pytest.mark.integration
def test_advertising_contract_matrix_and_all_exports(settings, tmp_path):
    """Реальные адаптеры сохраняют 41 сценарий, 15 контролей, исходники и редактируемый Word."""
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
    assert len(matrix["changes"]) == 41
    assert {"C14", "C15"} <= {control["id"] for control in matrix["unchanged_controls"]}, (
        "Обновите локальную expected_matrix.json: нужны C14/C15"
    )
    assert matrix["summary"]["controls"] == len(matrix["unchanged_controls"]) == 15
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
        if control["id"] in {"C14", "C15"}:
            assert_unchanged_fragment(result, control)
            continue
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
    import io
    from types import SimpleNamespace

    from pypdf import PdfReader

    projection = comparison_report(
        asdict(result), SimpleNamespace(ai_full=False, ai_unverified=False)
    )
    report = DocumentExporter(None).report(projection)
    report_text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(report)).pages)
    preamble = next(c for c in matrix["unchanged_controls"] if c["id"] == "C14")
    assert not re.search(
        r"\s+".join(re.escape(part) for part in preamble["value"].split()), report_text
    )
    assert not new.graphics
    assert not any("Диадок" in d.old_text + d.new_text for d in result.differences)
    deleted = next(
        d for d in result.differences if d.category == "text_deleted" and d.old_section == "8.5"
    )
    assert correspondence(asdict(result), "old", 6, deleted.id)["page"] == 4
    data = asdict(result)
    for difference in result.differences:
        if not difference.active or difference.category not in {"text_added", "text_deleted"}:
            continue
        side = "new" if difference.category == "text_added" else "old"
        areas = getattr(difference, side + "_regions")
        if areas:
            target = correspondence(data, side, areas[0].page, difference.id)
            other = "old" if side == "new" else "new"
            assert target["page"] and 1 <= target["page"] <= len(getattr(result, other).pages), (
                difference.id,
                target,
            )
    artifacts = DocumentExporter(LibreOffice(settings)).export(result, tmp_path, lambda _: None)
    assert all(e.path for side in artifacts.values() for e in side)
    word = next(e for e in artifacts["old"] if e.format == "docx")
    assert word.marked_changes > 0
    from zipfile import ZipFile

    from lxml import etree

    shades = []
    for path in (tmp_path / "old/accepted.docx", Path(word.path)):
        with ZipFile(path) as archive:
            root = etree.fromstring(archive.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraph = next(
            p
            for p in root.findall(".//w:p", ns)
            if re.search(
                r"\s+".join(re.escape(part) for part in preamble["value"].split()),
                "".join(p.itertext()),
            )
        )
        shades.append([etree.tostring(node) for node in paragraph.findall(".//w:shd", ns)])
    assert shades[0] == shades[1], "Общая преамбула не должна получать заливку DOCX"
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in hashes.items())
