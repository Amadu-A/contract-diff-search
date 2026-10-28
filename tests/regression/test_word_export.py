# tests/regression/test_word_export.py
"""Редактируемый исходный OOXML: частичная безопасная разметка без зависимости от PDF-геометрии."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from lxml import etree
from pypdf.errors import PdfReadError

from app.domain.comparison import compare
from app.domain.models import Difference, DocumentError, DocumentVersion, Page
from app.infrastructure.exports import DocumentExporter, shade_docx
from app.infrastructure.word import NS, read_docx, tag
from tests.factories import docx


@pytest.fixture
def word_case(tmp_path):
    """Создаёт Word-пакет с таблицей, разными стилями, управляющими runs и неизменными вложениями."""
    extra = '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Комплекс </w:t><w:tab/><w:t>77</w:t></w:r><w:r><w:t> после 88</w:t></w:r></w:p><w:p><w:r><w:t>Повтор</w:t></w:r></w:p><w:p><w:r><w:t>Повтор</w:t></w:r></w:p><w:tbl><w:tblPr><w:tblW w:w="6000" w:type="dxa"/></w:tblPr><w:tr><w:tc><w:tcPr><w:tcW w:w="6000" w:type="dxa"/></w:tcPr><w:p><w:r><w:rPr><w:i/></w:rPr><w:t>Таблица 123</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
    versions = []
    for side in ("old", "new"):
        folder = tmp_path / side
        folder.mkdir()
        source = docx(folder / "accepted.docx", ["2.2. Аванс 70%, срок 30 дней."], extra)
        with ZipFile(source, "a", ZIP_DEFLATED) as package:
            package.writestr("word/media/preserved.bin", b"original-image-content")
            package.writestr(
                "word/styles.xml",
                '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style w:type="paragraph" w:styleId="Test"><w:name w:val="Исходный стиль"/></w:style></w:styles>',
            )
        versions.append(
            DocumentVersion(
                side + ".docx",
                "docx",
                str(folder / "unused.pdf"),
                read_docx(source),
                [Page(1, 600, 850, "text", 1)],
            )
        )
    result = compare(*versions)
    result.differences = []
    return result, tmp_path


def change(result, identifier, value, *, status="confirmed", source="deterministic", element=0):
    """Строит независимый логический диапазон; слова и PDF-геометрия намеренно отсутствуют."""
    node = result.old.elements[element]
    start = node.text.index(value)
    return Difference(
        identifier,
        "number_modified",
        "Изменение",
        value,
        value,
        old_element=node.id,
        new_element=node.id,
        old_range=(start, start + len(value)),
        new_range=(start, start + len(value)),
        status=status,
        source=source,
    )


def shaded_text(path):
    """Читает фактические серые runs результирующего Word, включая табличные ячейки."""
    with ZipFile(path) as package:
        root = etree.fromstring(package.read("word/document.xml"))
        return [
            "".join(run.itertext())
            for run in root.findall(".//w:r", NS)
            if any(node.get(tag("fill")) == "D4D8DE" for node in run.findall("w:rPr/w:shd", NS))
        ]


def test_only_confirmed_changes_and_ai_are_gray_without_pdf_geometry(word_case):
    """Одинаковый DOCX содержит только подтверждённые факты, независимо от переключателя PDF."""
    result, folder = word_case
    result.differences = [
        change(result, "confirmed", "70%"),
        change(result, "ai", "30", source="ai"),
        change(result, "unverified", "Аванс", status="unverified"),
        change(result, "hypothesis", "срок", status="needs_review", source="ai"),
        change(result, "ambiguous", "дней", status="ambiguous"),
    ]
    artifact = DocumentExporter(None)._word(result, "old", folder, lambda stage: None)
    assert artifact.path and artifact.confirmed_only
    assert artifact.marked_changes == 2 and artifact.unmarked_changes == 0
    assert shaded_text(artifact.path) == ["70%", "30"]
    with ZipFile(artifact.path) as package:
        xml = package.read("word/document.xml")
        assert b"FFF0D0" not in xml and b"E5D9FF" not in xml
        assert package.read("word/media/preserved.bin") == b"original-image-content"
        assert package.read("word/styles.xml") == ZipFile(folder / "old/accepted.docx").read(
            "word/styles.xml"
        )


def test_one_unsafe_run_and_ambiguous_anchor_only_skip_their_own_shading(word_case):
    """Частичный сложный run и повторяющееся слово не блокируют обычную и табличную заливки."""
    result, folder = word_case
    result.differences = [
        change(result, "safe", "70%"),
        change(result, "unsafe", "77", element=1),
        change(result, "after-tab", "88", element=1),
        change(result, "cell", "123", element=4),
        Difference("ambiguous", "text_modified", "Без якоря", "Повтор", "Повтор"),
    ]
    source = folder / "old/accepted.docx"
    before = source.read_bytes()
    artifact = DocumentExporter(None)._word(result, "old", folder, lambda stage: None)
    assert artifact.marked_changes == 3 and artifact.unmarked_changes == 2
    assert "2 из 5" in artifact.reason
    assert shaded_text(artifact.path) == ["70%", "88", "123"]
    assert [e.text for e in read_docx(Path(artifact.path))] == [e.text for e in read_docx(source)]
    assert source.read_bytes() == before
    with ZipFile(artifact.path) as package:
        root = etree.fromstring(package.read("word/document.xml"))
        assert len(root.findall(".//w:tab", NS)) == 1
        assert root.findall(".//w:tbl", NS) and root.findall(".//w:rPr/w:i", NS)


@pytest.mark.parametrize("error", [DocumentError, OSError, PdfReadError])
def test_no_pdf_render_gate_and_word_survives_pdf_failure(word_case, monkeypatch, error):
    """Ошибка PDF и любой дрейф его геометрии не запрещают редактируемый DOCX."""
    result, folder = word_case
    result.differences = [change(result, "safe", "70%")]

    def fail(*args, **kwargs):
        """Имитирует недоступный PDF-экспорт независимо от исходного Word-пакета."""
        raise error("Ошибка PDF")

    monkeypatch.setattr("app.infrastructure.exports._pdf_export", fail)
    monkeypatch.setattr("app.infrastructure.exports.same_layout", lambda *args: False)
    artifacts = DocumentExporter(None).export(result, folder, lambda stage: None)
    for side in ("old", "new"):
        assert all(entry.path is None for entry in artifacts[side] if entry.format == "pdf")
        word = next(entry for entry in artifacts[side] if entry.format == "docx")
        assert word.path and word.marked_changes == 1


def test_word_failure_preserves_both_pdf_variants(word_case, monkeypatch):
    """Обратный отказ Word не удаляет и не ограничивает сформированные PDF."""
    result, folder = word_case

    def pdf_ok(result, side, target, checkpoint, show):
        """Регистрирует оба варианта существующего PDF-порта без конвертации Word."""
        target.write_bytes(b"synthetic-pdf-port")

    def fail(*args):
        """Имитирует ошибку отдельного Word-артефакта."""
        raise DocumentError("Ошибка Word")

    exporter = DocumentExporter(None)
    monkeypatch.setattr("app.infrastructure.exports._pdf_export", pdf_ok)
    monkeypatch.setattr(exporter, "_word", fail)
    artifacts = exporter.export(result, folder, lambda stage: None)
    for items in artifacts.values():
        assert [entry.show_unverified for entry in items if entry.format == "pdf"] == [True, False]
        assert all(entry.path for entry in items if entry.format == "pdf")
        assert next(entry for entry in items if entry.format == "docx").path is None


def test_full_complex_run_keeps_controls_and_partial_range_is_reported(word_case):
    """Целый сложный run допускает заливку без разделения, частичный — явный пропуск."""
    _, folder = word_case
    source = folder / "old/accepted.docx"
    target = folder / "complex.docx"
    skipped = shade_docx(
        source, target, {("word/document.xml", 1): [(0, len("Комплекс \t77")), (1, 2)]}
    )
    assert skipped == {("word/document.xml", 1, 1, 2)}
    with ZipFile(target) as package:
        root = etree.fromstring(package.read("word/document.xml"))
        assert root.findall(".//w:tab", NS) and root.findall(".//w:rPr/w:b", NS)


def test_global_content_guard_returns_original_and_reports_all_unmarked(word_case, monkeypatch):
    """При неожиданной потере структуры возвращается исходный редактируемый пакет без ложной полной разметки."""
    result, folder = word_case
    result.differences = [change(result, "safe", "70%")]
    monkeypatch.setattr("app.infrastructure.exports._same_word_content", lambda *args: False)
    artifact = DocumentExporter(None)._word(result, "old", folder, lambda stage: None)
    assert artifact.marked_changes == 0 and artifact.unmarked_changes == 1
    assert Path(artifact.path).read_bytes() == (folder / "old/accepted.docx").read_bytes()
