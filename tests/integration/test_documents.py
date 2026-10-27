# tests/integration/test_documents.py
"""Реальные PDF/OCR/LibreOffice адаптеры на синтетических документах всех форматов."""

import shutil
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, ByteStringObject, DictionaryObject, NameObject, NumberObject
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.domain.comparison import compare
from app.domain.models import DocumentError
from app.infrastructure.exports import DocumentExporter
from app.infrastructure.graphics import fingerprint, ssim
from app.infrastructure.ocr import TesseractOCR
from app.infrastructure.pdf import PdfDocumentExtractor, canonical_pdf, has_signature, render_page
from app.infrastructure.verifier import SharedVlmVerifier
from app.infrastructure.word import LibreOffice, prepare_docx, read_docx
from tests.factories import docx, pdf, version


def checkpoint(stage):
    """Принимает реальные этапы адаптеров без создания фиктивного прогресса."""
    return None


@pytest.fixture
def extractor(settings):
    """Собирает реальные адаптеры с изолированным каталогом теста."""
    return PdfDocumentExtractor(settings, TesseractOCR(settings), LibreOffice(settings))


def test_native_pdf_geometry_and_highlight_export(settings, extractor, tmp_path):
    """Цифра локализуется в обоих PDF; серый экспорт сохраняет весь исходный текст."""
    a = pdf(tmp_path / "a.pdf", ["2.2. Advance payment is 70%."])
    b = pdf(tmp_path / "b.pdf", ["2.2. Advance payment is 50%."])
    old = extractor.extract(a, "a.pdf", tmp_path / "old", False, checkpoint)
    new = extractor.extract(b, "b.pdf", tmp_path / "new", False, checkpoint)
    result = compare(old, new)
    difference = next(d for d in result.differences if d.category == "number_modified")
    assert difference.old_text == "70%" and difference.new_text == "50%"
    assert 100 < difference.old_regions[0].x0 < 400
    assert 60 < difference.old_regions[0].y0 < 100
    artifacts = DocumentExporter(LibreOffice(settings)).export(result, tmp_path, checkpoint)
    for side in ("old", "new"):
        path = Path(artifacts[side][0].path)
        assert len(PdfReader(path).pages) == 1
        assert "Advance payment" in PdfReader(path).pages[0].extract_text()
        assert b" re" in PdfReader(path).pages[0].get_contents().get_data()


def test_rotation_crop_and_three_hundred_page_limit(tmp_path):
    """CropBox/Rotate переходят в видимую геометрию; лимит проверяется на 300/301 страницах."""
    source = pdf(tmp_path / "rotated.pdf", ["Visible crop"])
    reader = PdfReader(source)
    page = reader.pages[0]
    page.cropbox.lower_left = (20, 30)
    page.cropbox.upper_right = (575, 812)
    page.rotate(90)
    writer = PdfWriter()
    writer.add_page(page)
    with source.open("wb") as stream:
        writer.write(stream)
    canonical_pdf(source, tmp_path / "normalized.pdf", 300)
    normalized = PdfReader(tmp_path / "normalized.pdf").pages[0]
    assert float(normalized.mediabox.width) == 782
    assert float(normalized.mediabox.height) == 555
    assert normalized.rotation == 0
    many = pdf(tmp_path / "300.pdf", ["Page"], 300)
    canonical_pdf(many, tmp_path / "300-copy.pdf", 300)
    assert len(PdfReader(tmp_path / "300-copy.pdf").pages) == 300
    too_many = pdf(tmp_path / "301.pdf", ["Page"], 301)
    with pytest.raises(DocumentError):
        canonical_pdf(too_many, tmp_path / "rejected.pdf", 300)


def test_crypto_signature_objects_not_ink(tmp_path):
    """Рисунок с подписью не считается электронной подписью; /Sig-объект определяется."""
    path = pdf(tmp_path / "ink.pdf", ["Signature and seal"])
    assert not has_signature(path)
    writer = PdfWriter()
    writer.add_blank_page(595, 842)
    signature = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Sig"),
            NameObject("/ByteRange"): ArrayObject(
                [NumberObject(0), NumberObject(10), NumberObject(20), NumberObject(10)]
            ),
            NameObject("/Contents"): ByteStringObject(b"synthetic-signature"),
        }
    )
    field = DictionaryObject({NameObject("/FT"): NameObject("/Sig"), NameObject("/V"): signature})
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject(
        {NameObject("/Fields"): ArrayObject([writer._add_object(field)])}
    )
    signed = tmp_path / "signed.pdf"
    with signed.open("wb") as stream:
        writer.write(stream)
    assert has_signature(signed)


def test_scanned_page_ocr_and_missing_ocr_warning(settings, extractor, tmp_path):
    """Пустой PDF-слой запускает OCR без ИИ, а его отказ даёт непроверенную страницу."""
    native = pdf(
        tmp_path / "native.pdf", ["2.2. Advance payment is 50%.", "5.2. Penalty cap 0.01%."]
    )
    image = render_page(native, 1, 3)
    scanned = tmp_path / "scan.pdf"
    drawing = canvas.Canvas(str(scanned), pagesize=(595, 842))
    drawing.drawImage(ImageReader(image), 0, 0, 595, 842)
    drawing.showPage()
    drawing.save()
    image.close()
    assert PdfReader(scanned).pages[0].extract_text() == ""
    if shutil.which(settings.tesseract_command):
        result = extractor.extract(scanned, "scan.pdf", tmp_path / "ocr", False, checkpoint)
        assert any("50%" in e.text for e in result.elements)
        assert any(e.words for e in result.elements)
        assert result.pages[0].mode == "scanned"
    missing = settings.model_copy(update={"tesseract_command": "definitely-missing-tesseract"})
    bad = PdfDocumentExtractor(missing, TesseractOCR(missing), LibreOffice(missing)).extract(
        scanned, "scan.pdf", tmp_path / "missing", False, checkpoint
    )
    assert not bad.elements and bad.pages[0].warnings


def test_tracked_changes_nested_tables_and_numbering(settings, tmp_path):
    """OOXML применяет исправления и вычисляет начальные значения родительских уровней."""
    extra = '<w:p><w:del><w:r><w:delText>Удалено</w:delText></w:r></w:del><w:ins><w:r><w:t>Принято</w:t></w:r></w:ins></w:p><w:p><w:pPr><w:numPr><w:ilvl w:val="2"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Список</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Ячейка</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Вложенная</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:tc></w:tr></w:tbl>'
    numbering = '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="5"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl><w:lvl w:ilvl="1"><w:start w:val="5"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2."/></w:lvl><w:lvl w:ilvl="2"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2.%3."/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>'
    original = docx(tmp_path / "changes.docx", ["Начало"], extra, numbering)
    content = original.read_bytes()
    accepted = tmp_path / "accepted.docx"
    prepare_docx(original, accepted, settings)
    elements = read_docx(accepted)
    assert [e.text for e in elements] == [
        "Начало",
        "Принято",
        "5.5.1. Список",
        "Ячейка",
        "Вложенная",
    ]
    assert all(e.kind == "cell" for e in elements[-2:])
    assert original.read_bytes() == content


@pytest.mark.parametrize(
    "old_format,new_format",
    [(a, b) for a in ("doc", "docx", "pdf") for b in ("doc", "docx", "pdf")],
)
def test_all_nine_format_combinations(settings, extractor, tmp_path, old_format, new_format):
    """Каждая из девяти пар реально конвертируется и обнаруживает заданное число."""
    if not shutil.which(settings.soffice_command):
        pytest.skip("LibreOffice не установлен; Linux-контейнер включает его")
    office = LibreOffice(settings)
    files = {}
    for side, number in (("old", "70"), ("new", "50")):
        original = docx(tmp_path / f"{side}.docx", [f"2.2. Advance payment is {number}%."])
        kind = old_format if side == "old" else new_format
        files[side] = (
            original
            if kind == "docx"
            else office.convert(original, tmp_path / f"convert-{side}", kind, checkpoint)
        )
    a = extractor.extract(files["old"], "our." + old_format, tmp_path / "old", False, checkpoint)
    b = extractor.extract(
        files["new"], "partner." + new_format, tmp_path / "new", False, checkpoint
    )
    assert any(d.old_text == "70%" and d.new_text == "50%" for d in compare(a, b).differences)


def test_phash_ssim_detect_small_local_edit():
    """Общий perceptual hash не скрывает маленькую изменённую цифру на рисунке."""
    a = Image.new("RGB", (200, 100), "white")
    ImageDraw.Draw(a).rectangle((20, 20, 180, 80), outline="blue", width=3)
    b = a.copy()
    ImageDraw.Draw(b).text((90, 50), "7", fill="black")
    assert fingerprint(a)[0] != fingerprint(b)[0]
    assert ssim(a, b) < 1
    a.close()
    b.close()


def test_vlm_validates_response_without_credentials(settings):
    """Некорректный JSON не меняет результат; endpoint и модель логические, Authorization отсутствует."""
    result = compare(version(["1. Unique old words."]), version(["1. Unique new words."]))

    def handler(request):
        """Проверяет фактический HTTP-контракт без вызова стороннего облака."""
        assert "authorization" not in request.headers
        assert b'"model":"shared-vlm"' in request.content
        assert b'"enable_thinking":false' in request.content
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"outcome":"same","readable":true,"findings":[]}'}}
                ]
            },
        )

    result.old.elements[0].words = []
    result.new.elements[0].words = []
    from app.domain.models import ReviewUnit

    reply = SharedVlmVerifier(settings, httpx.MockTransport(handler)).review(
        ReviewUnit("test", "full", "text"), result.old, result.new
    )
    assert reply.outcome == "same" and reply.findings == []


def test_word_export_is_real_and_keeps_layout(settings, extractor, tmp_path):
    """Простой DOCX получает настоящий проверенный экспорт с неизменной вёрсткой."""
    if not shutil.which(settings.soffice_command):
        pytest.skip("Нужен LibreOffice")
    a = docx(tmp_path / "a.docx", ["2.2. Advance payment is 70%."])
    b = docx(tmp_path / "b.docx", ["2.2. Advance payment is 50%."])
    original_hash = a.read_bytes()
    old = extractor.extract(a, "a.docx", tmp_path / "old", False, checkpoint)
    new = extractor.extract(b, "b.docx", tmp_path / "new", False, checkpoint)
    artifacts = DocumentExporter(LibreOffice(settings)).export(
        compare(old, new), tmp_path, checkpoint
    )
    word = next(item for item in artifacts["old"] if item.format == "docx")
    assert word.path, word.reason
    assert [e.text for e in read_docx(Path(word.path))] == [
        e.text for e in read_docx(tmp_path / "old/accepted.docx")
    ]
    assert a.read_bytes() == original_hash
