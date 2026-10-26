# tests/security/test_uploads.py
"""Злонамеренные архивы, ложные типы, защита PDF и ограничения загрузки."""

import io
from uuid import uuid4
from zipfile import ZipFile

import pytest
from pypdf import PdfWriter

from app.domain.models import DocumentError
from app.infrastructure.storage import FileStorage, safe_xml, validate_docx
from tests.factories import docx


@pytest.mark.parametrize(
    "name",
    [
        "../outside.xml",
        "/absolute.xml",
        "word/../../outside",
        "C:/outside.xml",
        "word\\outside.xml",
    ],
)
def test_zip_paths_are_not_extracted(settings, tmp_path, name):
    """Ни traversal, ни Windows-пути не допускаются даже без распаковки на диск."""
    path = docx(tmp_path / "danger.docx", ["Безопасный текст"])
    with ZipFile(path, "a") as archive:
        archive.writestr(name, "bad")
    with pytest.raises(DocumentError):
        validate_docx(path, settings)


def test_external_entities_macros_and_resources_are_rejected(settings, tmp_path):
    """XML-сущности, VBA и внешние картинки не выполняются и не загружаются."""
    with pytest.raises(DocumentError):
        safe_xml(b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///secret">]><x>&a;</x>')
    for member, data in [
        ("word/vbaProject.bin", b"macro"),
        (
            "word/_rels/document.xml.rels",
            b'<Relationships><Relationship TargetMode="External" Type="image" Target="https://attacker.invalid"/></Relationships>',
        ),
    ]:
        path = docx(tmp_path / "bad.docx", ["text"])
        with ZipFile(path, "a") as archive:
            archive.writestr(member, data)
        with pytest.raises(DocumentError):
            validate_docx(path, settings)


def test_encrypted_pdf_and_fake_extension(settings):
    """Защищённый PDF отклоняется без попытки подбора пароля; MIME не заменяет magic."""
    writer = PdfWriter()
    writer.add_blank_page(595, 842)
    writer.encrypt("private-password")
    stream = io.BytesIO()
    writer.write(stream)
    storage = FileStorage(settings)
    with pytest.raises(DocumentError, match="парольной"):
        storage.save_upload(str(uuid4()), "old", "encrypted.pdf", io.BytesIO(stream.getvalue()))
    with pytest.raises(DocumentError, match="формат"):
        storage.save_upload(str(uuid4()), "old", "fake.docx", io.BytesIO(b"%PDF-1.7"))


def test_size_and_unpack_limits(settings, tmp_path):
    """Лимиты проверяются по фактическим байтам и суммарному размеру XML."""
    settings.max_file_mb = 1
    with pytest.raises(DocumentError, match="размер"):
        FileStorage(settings).save_upload(
            str(uuid4()), "old", "big.pdf", io.BytesIO(b"x" * (1024**2 + 1))
        )
    path = docx(tmp_path / "bomb.docx", ["x" * (2 * 1024**2)])
    settings.max_unpacked_mb = 1
    with pytest.raises(DocumentError, match="распаковки"):
        validate_docx(path, settings)
