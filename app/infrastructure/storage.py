# app/infrastructure/storage.py
"""Безопасное временное файловое хранилище и проверка контейнеров DOCX/PDF."""

import json
import shutil
import stat
import time
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from uuid import UUID
from zipfile import BadZipFile, ZipFile

import olefile
from lxml import etree
from pydantic import TypeAdapter
from pypdf import PdfReader

from app.domain.models import ComparisonResult, DocumentError
from app.settings import Settings


def safe_xml(data: bytes):
    """Запрещает DTD, сетевые сущности и расширение XML-бомб."""
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise DocumentError("DTD и XML-сущности в документе запрещены")
    return etree.fromstring(
        data, etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    )


def validate_docx(path: Path, settings: Settings) -> None:
    """Проверяет пути, распаковку, макросы и внешние ресурсы без извлечения архива."""
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 10000 or len({i.filename for i in infos}) != len(infos):
                raise DocumentError("Недопустимая структура архива DOCX")
            if sum(i.file_size for i in infos) > settings.max_unpacked_mb * 1024**2:
                raise DocumentError("Превышен лимит распаковки DOCX")
            names = {i.filename for i in infos}
            if not {"[Content_Types].xml", "word/document.xml"} <= names:
                raise DocumentError("Файл не является документом DOCX")
            for info in infos:
                name, mode = PurePosixPath(info.filename), info.external_attr >> 16
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or "\\" in info.filename
                    or ":" in info.filename
                    or stat.S_ISLNK(mode)
                ):
                    raise DocumentError("Небезопасный путь внутри DOCX")
                if "vbaproject" in info.filename.lower() or "/embeddings/" in info.filename.lower():
                    raise DocumentError("Макросы и вложенные исполняемые объекты не поддерживаются")
                if info.filename.endswith((".xml", ".rels")):
                    if info.file_size > settings.max_xml_mb * 1024**2:
                        raise DocumentError("Превышен лимит XML-части DOCX")
                    root = safe_xml(archive.read(info))
                    if info.filename.endswith(".rels"):
                        for rel in root:
                            if rel.get("TargetMode") == "External" and not rel.get(
                                "Type", ""
                            ).endswith("/hyperlink"):
                                raise DocumentError(
                                    "Внешние ресурсы DOCX не загружаются; удалите внешние связи"
                                )
            if archive.testzip():
                raise DocumentError("Повреждён архив DOCX")
    except (BadZipFile, etree.XMLSyntaxError, KeyError) as exc:
        raise DocumentError("Повреждён документ DOCX") from exc


class FileStorage:
    """Хранит оригиналы под фиксированными именами внутри UUID-каталогов."""

    def __init__(self, settings: Settings):
        """Закрепляет корень данных и создаёт его до начала приёма файлов."""
        self.settings = settings
        self.root = settings.data_dir.resolve() / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, job_id: str) -> Path:
        """Проверяет UUID и границу корня перед файловыми операциями."""
        if str(UUID(job_id)) != job_id:
            raise DocumentError("Некорректный идентификатор задания")
        path = (self.root / job_id).resolve()
        if path.parent != self.root:
            raise DocumentError("Недопустимый каталог задания")
        return path

    def save_upload(self, job_id: str, side: str, name: str, stream: BinaryIO) -> str:
        """Копирует потоком с лимитом и проверяет формат до публикации задачи."""
        extension = Path(name).suffix.lower()
        if extension not in {".doc", ".docx", ".pdf"} or side not in {"old", "new"}:
            raise DocumentError("Допустимы только DOC, DOCX и PDF")
        directory = self.directory(job_id)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path, size = directory / f"{side}{extension}", 0
        if shutil.disk_usage(directory).free < self.settings.max_file_mb * 3 * 1024**2:
            raise DocumentError("Недостаточно свободного места для обработки")
        with path.open("xb") as output:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > self.settings.max_file_mb * 1024**2:
                    raise DocumentError("Превышен допустимый размер файла")
                output.write(chunk)
        with path.open("rb") as source:
            magic = source.read(16)
        if extension == ".docx" and magic.startswith(b"PK"):
            validate_docx(path, self.settings)
        elif extension == ".pdf" and magic.startswith(b"%PDF-"):
            try:
                reader = PdfReader(path)
                if reader.is_encrypted:
                    raise DocumentError("PDF с парольной защитой не поддерживается")
                if not 0 < len(reader.pages) <= self.settings.max_pages:
                    raise DocumentError("Превышено допустимое число страниц либо PDF пуст")
            except DocumentError:
                raise
            except Exception as exc:
                raise DocumentError("Повреждён PDF") from exc
        elif extension == ".doc" and magic.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
            try:
                with olefile.OleFileIO(path, raise_defects=olefile.DEFECT_INCORRECT) as compound:
                    streams = ["/".join(parts) for parts in compound.listdir()]
                    if "WordDocument" not in streams or not any(
                        name in streams for name in ("0Table", "1Table")
                    ):
                        raise DocumentError("Файл не является бинарным документом Word")
                    if any("vba" in name.lower() or "macros" in name.lower() for name in streams):
                        raise DocumentError("Макросы в DOC не поддерживаются")
            except DocumentError:
                raise
            except Exception as exc:
                raise DocumentError("Повреждён бинарный DOC") from exc
        else:
            raise DocumentError("Фактический формат файла не совпадает с расширением")
        return extension[1:]

    def delete(self, job_id: str) -> None:
        """Удаляет только проверенный каталог; повторный вызов безопасен."""
        path = self.directory(job_id)
        if path.exists():
            shutil.rmtree(path)

    def write_result(self, job_id: str, result: ComparisonResult) -> None:
        """Публикует JSON атомарным переименованием, исключая частичный ответ API."""
        path = self.directory(job_id)
        temporary = path / "result.tmp"
        temporary.write_text(json.dumps(asdict(result), ensure_ascii=False), encoding="utf-8")
        temporary.replace(path / "result.json")

    def read_result(self, job_id: str) -> dict:
        """Читает только результат указанного задания; доступ проверяет use-case."""
        return json.loads((self.directory(job_id) / "result.json").read_text(encoding="utf-8"))

    def has_result(self, job_id: str) -> bool:
        """Проверяет наличие атомарного checkpoint базового сравнения."""
        return (self.directory(job_id) / "result.json").is_file()

    def restore_result(self, job_id: str) -> ComparisonResult:
        """Восстанавливает типизированный checkpoint без повторного чтения/OCR оригиналов."""
        return TypeAdapter(ComparisonResult).validate_python(self.read_result(job_id))

    def cleanup_transients(self, older_than: float) -> None:
        """Чистит только lo-профили заданного корня, не затрагивая активную конвертацию."""
        root = self.settings.process_temp_dir.resolve()
        if not root.exists():
            return
        threshold = min(older_than, time.time() - self.settings.process_timeout_seconds - 30)
        for entry in root.glob("lo-*"):
            if entry.is_symlink() or not entry.is_dir() or entry.stat().st_mtime >= threshold:
                continue
            target = entry.resolve()
            if target.parent == root:
                shutil.rmtree(target)
