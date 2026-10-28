# app/infrastructure/word.py
"""Чтение итогового OOXML и изолированный рендеринг Word через LibreOffice.

Удалённые редакции не участвуют в сравнении. Неподдерживаемые сложные
исправления завершаются понятной ошибкой вместо неверной итоговой версии.
"""

import os
import re
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree

from app.domain.models import DocumentElement, DocumentError, TextRun
from app.domain.text import SECTION
from app.infrastructure.processes import run_process
from app.infrastructure.storage import safe_xml, validate_docx
from app.settings import Settings

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W, "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def tag(name: str) -> str:
    """Формирует полное имя элемента WordprocessingML."""
    return f"{{{W}}}{name}"


def accept_changes(root) -> None:
    """Применяет вставки/удаления к копии XML, включая строки и границы абзацев."""
    if root.findall(".//w:cellMerge", NS) or root.findall(".//w:altChunk", NS):
        raise DocumentError(
            "Сложное объединение ячеек или altChunk требует принятия исправлений в Word"
        )
    for paragraph in list(root.findall(".//w:p", NS)):
        if paragraph.find("w:pPr/w:rPr/w:del", NS) is not None:
            following = paragraph.getnext()
            if following is None or following.tag != tag("p"):
                raise DocumentError("Не удалось безопасно применить удаление границы абзаца")
            for child in list(following):
                if child.tag != tag("pPr"):
                    paragraph.append(child)
            following.getparent().remove(following)
    for row in list(root.findall(".//w:tr", NS)):
        if row.find("w:trPr/w:del", NS) is not None:
            row.getparent().remove(row)
    for cell in list(root.findall(".//w:tc", NS)):
        if cell.find("w:tcPr/w:cellDel", NS) is not None:
            cell.getparent().remove(cell)
    removed = {
        tag(t)
        for t in (
            "del",
            "moveFrom",
            "rPrChange",
            "pPrChange",
            "tblPrChange",
            "trPrChange",
            "tcPrChange",
            "sectPrChange",
            "numberingChange",
        )
    }
    unwrapped = {tag("ins"), tag("moveTo")}
    for node in list(root.iter()):
        parent = node.getparent()
        if parent is None:
            continue
        if node.tag in removed:
            parent.remove(node)
        elif node.tag in unwrapped:
            position = parent.index(node)
            for child in list(node):
                parent.insert(position, child)
                position += 1
            parent.remove(node)


def prepare_docx(source: Path, target: Path, settings: Settings) -> None:
    """Создаёт итоговую безопасную копию, не изменяя оригинальные байты."""
    validate_docx(source, settings)
    with ZipFile(source) as original, ZipFile(target, "w", ZIP_DEFLATED) as output:
        for info in original.infolist():
            data = original.read(info)
            if info.filename.startswith("word/") and info.filename.endswith(".xml"):
                root = safe_xml(data)
                for field in root.findall(".//w:instrText", NS):
                    if re.search(
                        r"\b(DDE|DDEAUTO|INCLUDETEXT|INCLUDEPICTURE|LINK)\b", field.text or "", re.I
                    ):
                        raise DocumentError("Внешние поля Word не поддерживаются")
                accept_changes(root)
                data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            output.writestr(info, data)


class Numbering:
    """Вычисляет отображаемые номера списков с уровнями и startOverride."""

    def __init__(self, archive: ZipFile):
        """Читает numId/abstractNumId один раз для всего документа."""
        self.abstract, self.nums, self.counts, self.overrides = {}, {}, defaultdict(dict), {}
        if "word/numbering.xml" not in archive.namelist():
            return
        root = safe_xml(archive.read("word/numbering.xml"))
        for abstract in root.findall("w:abstractNum", NS):
            self.abstract[abstract.get(tag("abstractNumId"))] = {
                int(level.get(tag("ilvl"))): level for level in abstract.findall("w:lvl", NS)
            }
        for num in root.findall("w:num", NS):
            key = num.get(tag("numId"))
            self.nums[key] = num.find("w:abstractNumId", NS).get(tag("val"))
            for override in num.findall("w:lvlOverride", NS):
                start = override.find("w:startOverride", NS)
                if start is not None:
                    self.overrides[(key, int(override.get(tag("ilvl"))))] = int(
                        start.get(tag("val"))
                    )

    def prefix(self, properties) -> str:
        """Возвращает номер очередного абзаца; неподдерживаемые форматы не угадывает."""
        num_pr = properties.find("w:numPr", NS) if properties is not None else None
        if num_pr is None:
            return ""
        num = num_pr.find("w:numId", NS)
        level = num_pr.find("w:ilvl", NS)
        key, depth = num.get(tag("val")), int(level.get(tag("val"))) if level is not None else 0
        if key == "0":
            return ""
        definitions = self.abstract.get(self.nums.get(key), {})
        definition = definitions.get(depth)
        if definition is None:
            raise DocumentError("Не удалось вычислить автоматическую нумерацию")
        fmt = definition.find("w:numFmt", NS)
        fmt = fmt.get(tag("val")) if fmt is not None else "decimal"
        text = definition.find("w:lvlText", NS)
        template = text.get(tag("val")) if text is not None else f"%{depth + 1}."
        counts = self.counts[key]
        start_node = definition.find("w:start", NS)
        start = self.overrides.get(
            (key, depth), int(start_node.get(tag("val"))) if start_node is not None else 1
        )
        counts[depth] = counts.get(depth, start - 1) + 1
        for other in list(counts):
            if other > depth:
                del counts[other]
        if fmt == "bullet":
            return template + " "
        for index in range(depth + 1):
            level_def = definitions.get(index, definition)
            initial_node = level_def.find("w:start", NS)
            initial_value = int(initial_node.get(tag("val"))) if initial_node is not None else 1
            value = counts.get(index, self.overrides.get((key, index), initial_value))
            format_node = level_def.find("w:numFmt", NS)
            format_name = format_node.get(tag("val")) if format_node is not None else "decimal"
            rendered = _number(value, format_name)
            template = template.replace(f"%{index + 1}", rendered)
        return template + " "


def _number(value: int, fmt: str) -> str:
    """Форматирует десятичные, буквенные и римские номера без потери их вида."""
    if fmt in {"decimal", "decimalZero"}:
        return str(value).zfill(2 if fmt == "decimalZero" else 1)
    if fmt in {"lowerLetter", "upperLetter"}:
        result = ""
        while value:
            value, remainder = divmod(value - 1, 26)
            result = chr(97 + remainder) + result
        return result.upper() if fmt == "upperLetter" else result
    if fmt in {"lowerRoman", "upperRoman"}:
        result = ""
        for number, letter in (
            (1000, "M"),
            (900, "CM"),
            (500, "D"),
            (400, "CD"),
            (100, "C"),
            (90, "XC"),
            (50, "L"),
            (40, "XL"),
            (10, "X"),
            (9, "IX"),
            (5, "V"),
            (4, "IV"),
            (1, "I"),
        ):
            while value >= number:
                result += letter
                value -= number
        return result.lower() if fmt == "lowerRoman" else result
    raise DocumentError(f"Неподдерживаемый формат нумерации: {fmt}")


def _properties(node) -> dict[str, str]:
    """Нормализует согласованный набор признаков оформления OOXML."""
    result = {}
    if node is None:
        return result
    for name in ("b", "i", "u", "strike", "dstrike", "sz", "color", "highlight", "shd", "rFonts"):
        value = node.find(f"w:{name}", NS)
        if value is not None:
            result[name] = (
                ";".join(f"{etree.QName(k).localname}={v}" for k, v in sorted(value.attrib.items()))
                or "true"
            )
    return result


def read_docx(path: Path) -> list[DocumentElement]:
    """Читает абзацы в порядке OOXML, вложенные таблицы, примечания и колонтитулы."""
    result, section = [], ""
    with ZipFile(path) as archive:
        numbering = Numbering(archive)
        styles = {}
        defaults = {}
        if "word/styles.xml" in archive.namelist():
            styles_root = safe_xml(archive.read("word/styles.xml"))
            defaults = _properties(styles_root.find("w:docDefaults/w:rPrDefault/w:rPr", NS))
            for style in styles_root.findall("w:style", NS):
                styles[style.get(tag("styleId"))] = style
        parts = ["word/document.xml"] + sorted(
            n
            for n in archive.namelist()
            if re.match(r"word/(header\d+|footer\d+|footnotes|endnotes)\.xml$", n)
        )
        for part in parts:
            root = safe_xml(archive.read(part))
            rels_name = f"word/_rels/{Path(part).name}.rels"
            rels = (
                {r.get("Id"): r.get("Target", "") for r in safe_xml(archive.read(rels_name))}
                if rels_name in archive.namelist()
                else {}
            )
            for index, paragraph in enumerate(root.findall(".//w:p", NS)):
                # Специальные разделители сносок не являются пользовательским содержимым.
                note = next(
                    (
                        a
                        for a in paragraph.iterancestors()
                        if a.tag in {tag("footnote"), tag("endnote")}
                    ),
                    None,
                )
                if note is not None and int(note.get(tag("id"), "0")) <= 0:
                    continue
                ppr = paragraph.find("w:pPr", NS)
                inherited = defaults.copy()
                style_node = ppr.find("w:pStyle", NS) if ppr is not None else None
                style_id = style_node.get(tag("val")) if style_node is not None else None
                chain, seen = [], set()
                while style_id in styles and style_id not in seen:
                    seen.add(style_id)
                    style = styles[style_id]
                    chain.append(style)
                    parent = style.find("w:basedOn", NS)
                    style_id = parent.get(tag("val")) if parent is not None else None
                for style in reversed(chain):
                    inherited.update(_properties(style.find("w:rPr", NS)))
                effective_ppr = deepcopy(ppr) if ppr is not None else etree.Element(tag("pPr"))
                if effective_ppr.find("w:numPr", NS) is None:
                    for style in chain:
                        num = style.find("w:pPr/w:numPr", NS)
                        if num is not None:
                            effective_ppr.append(deepcopy(num))
                            break
                prefix = numbering.prefix(effective_ppr)
                text, runs = prefix, []
                for run in paragraph.findall(".//w:r", NS):
                    if next(run.iterancestors(tag("p")), None) is not paragraph:
                        continue
                    value = "".join(
                        (node.text or "")
                        if node.tag == tag("t")
                        else "\t"
                        if node.tag == tag("tab")
                        else "\n"
                        if node.tag in {tag("br"), tag("cr")}
                        else ""
                        for node in run
                    )
                    if value:
                        props = inherited | _properties(run.find("w:rPr", NS))
                        runs.append(
                            TextRun(len(text), len(text) + len(value), tuple(sorted(props.items())))
                        )
                        text += value
                if not text.strip():
                    continue
                number = SECTION.match(text)
                if number:
                    section = number.group(1)
                cell = next(paragraph.iterancestors(tag("tc")), None)
                kind = "cell" if cell is not None else "list_item" if prefix else "paragraph"
                attrs = {
                    "part": part,
                    "paragraph_index": str(index),
                    "prefix_length": str(len(prefix)),
                }
                if cell is not None:
                    cell_pr = cell.find("w:tcPr", NS)
                    attrs["grid"] = (
                        etree.tostring(cell_pr, encoding="unicode") if cell_pr is not None else ""
                    )
                    row = cell.getparent()
                    populated_cells = [
                        c for c in row.findall("w:tc", NS) if c.findall(".//w:t", NS)
                    ]
                    attrs["column"] = str(populated_cells.index(cell))
                    first = row.find("w:tc", NS)
                    attrs["row_key"] = "".join(first.itertext())[:120] if first is not None else ""
                    attrs["cell_path"] = root.getroottree().getpath(cell)
                links = [
                    "#" + link.get(tag("anchor"))
                    if link.get(tag("anchor"))
                    else rels.get(link.get(f"{{{NS['r']}}}id"), "")
                    for link in paragraph.findall(".//w:hyperlink", NS)
                ]
                if links:
                    attrs["hyperlinks"] = "\n".join(links)
                fields = [node.text or "" for node in paragraph.findall(".//w:instrText", NS)]
                fields.extend(
                    node.get(tag("instr"), "") for node in paragraph.findall(".//w:fldSimple", NS)
                )
                fields.extend(
                    "hidden:" + "".join(run.itertext())
                    for run in paragraph.findall(".//w:r", NS)
                    if run.find("w:rPr/w:vanish", NS) is not None
                )
                if fields:
                    attrs["hidden_fields"] = "\n".join(fields)
                result.append(
                    DocumentElement(
                        f"E{len(result):06d}",
                        kind,
                        text,
                        section,
                        f"{part}:p{index}",
                        attributes=attrs,
                        runs=runs,
                    )
                )
    return result


class LibreOffice:
    """Конвертация временных копий в отдельном профиле без макросов и обновления ссылок."""

    def __init__(self, settings: Settings):
        """Принимает команду и лимиты; никаких процессов при конструировании не запускает."""
        self.settings = settings

    def convert(
        self, source: Path, output: Path, extension: str, checkpoint: Callable[[str], None]
    ) -> Path:
        """Конвертирует один файл; ошибки и таймаут не выдаются за успешный результат."""
        output.mkdir(parents=True, exist_ok=True)
        self.settings.process_temp_dir.mkdir(parents=True, exist_ok=True)
        temporary_profile = tempfile.TemporaryDirectory(
            prefix="lo-", dir=self.settings.process_temp_dir
        )
        profile = Path(temporary_profile.name)
        user = profile / "user"
        user.mkdir()
        (user / "registrymodifications.xcu").write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
</oor:items>""",
            encoding="utf-8",
        )
        filters = {
            "pdf": "pdf:writer_pdf_Export",
            "docx": "docx:Office Open XML Text",
            "doc": "doc:MS Word 97",
        }
        try:
            command = self.settings.soffice_command
            if os.name == "nt":
                resolved = shutil.which(command)
                console = Path(resolved).with_suffix(".com") if resolved else None
                if console is not None and console.exists():
                    command = str(console)
            run_process(
                [
                    command,
                    f"-env:UserInstallation={profile.resolve().as_uri()}",
                    "--headless",
                    "--nologo",
                    "--nodefault",
                    "--nolockcheck",
                    "--norestore",
                    "--convert-to",
                    filters[extension],
                    "--outdir",
                    str(output.resolve()),
                    str(source.resolve()),
                ],
                output,
                self.settings.process_timeout_seconds,
                checkpoint,
                "Конвертация Word",
            )
            target = output / f"{source.stem}.{extension}"
            if not target.is_file() or target.stat().st_size == 0:
                raise DocumentError("LibreOffice не создал итоговый документ")
            return target
        finally:
            shutil.rmtree(profile, ignore_errors=True)
            temporary_profile.cleanup()
