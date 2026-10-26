# tests/factories.py
"""Синтетические документы с заранее заданными изменениями без реальных реквизитов."""

from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app.domain.models import DocumentElement, DocumentVersion, Page, VisualRegion, Word


def version(texts: list[str], format: str = "docx", shift: int = 0) -> DocumentVersion:
    """Создаёт независимую модель с точными областями каждого слова."""
    elements = []
    for i, text in enumerate(texts):
        words, start = [], 0
        for value in text.split():
            start = text.find(value, start)
            words.append(
                Word(
                    value,
                    start,
                    start + len(value),
                    VisualRegion(
                        i + 1 + shift, start * 3 + 30, 60, (start + len(value)) * 3 + 30, 72
                    ),
                )
            )
            start += len(value)
        section = text.split()[0].rstrip(".") if text and text[0].isdigit() else ""
        elements.append(DocumentElement(f"E{i:06d}", "paragraph", text, section, words=words))
    return DocumentVersion(
        "synthetic." + format,
        format,
        "unused.pdf",
        elements,
        [Page(i + 1, 600, 850, "text", 1.0) for i in range(len(texts) + shift)],
    )


def docx(path: Path, paragraphs: list[str], extra_xml: str = "", numbering: str = "") -> Path:
    """Пишет минимальный валидный DOCX; ожидаемое содержание задано самим тестом."""
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
        for text in paragraphs
    )
    body += extra_xml
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            + (
                '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
                if numbering
                else ""
            )
            + "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr></w:body></w:document>',
        )
        if numbering:
            archive.writestr("word/numbering.xml", numbering)
            archive.writestr(
                "word/_rels/document.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/></Relationships>',
            )
    return path


def font() -> str:
    """Находит системный кириллический шрифт; его бинарник не добавляется в Git."""
    if "TestCyrillic" in pdfmetrics.getRegisteredFontNames():
        return "TestCyrillic"
    for candidate in (
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf"),
    ):
        if candidate.exists():
            pdfmetrics.registerFont(TTFont("TestCyrillic", str(candidate)))
            return "TestCyrillic"
    return "Helvetica"


def pdf(path: Path, paragraphs: list[str], pages: int = 1) -> Path:
    """Создаёт текстовый PDF с известными координатами и числом страниц."""
    drawing = canvas.Canvas(str(path), pagesize=(595, 842))
    for _page in range(pages):
        drawing.setFont(font(), 12)
        for index, text in enumerate(paragraphs):
            drawing.drawString(60, 760 - index * 30, text)
        drawing.showPage()
    drawing.save()
    return path
