# app/infrastructure/reports.py
"""Многостраничный кириллический PDF «Было / Стало» по готовой прикладной проекции."""

import io
from datetime import datetime, timedelta, timezone
from functools import cache, partial
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    CondPageBreak,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.domain.models import DocumentError
from app.domain.visibility import is_unverified

BLUE = colors.HexColor("#246BDB")
INK = colors.HexColor("#172846")
MUTED = colors.HexColor("#587098")
LINE = colors.HexColor("#D6E3F3")
TYPES = {
    "number_modified": "Число",
    "text_modified": "Изменение",
    "text_added": "Добавление",
    "text_deleted": "Удаление",
    "text_moved": "Перемещение",
    "numbering_changed": "Нумерация",
    "formatting_changed": "Оформление",
    "structure_changed": "Структура",
    "table_modified": "Таблица",
    "table_structure_changed": "Структура таблицы",
    "graphic_added": "Добавлена графика",
    "graphic_deleted": "Удалена графика",
    "graphic_modified": "Изменена графика",
    "unverified_region": "Область",
    "ambiguous_match": "Соответствие",
    "ai_contradiction": "Противоречие ИИ",
}


@cache
def _fonts() -> None:
    """Встраивает доступный в образе или Windows шрифт с кириллицей; без сетевой загрузки."""
    for regular, bold in (
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ):
        if Path(regular).is_file() and Path(bold).is_file():
            pdfmetrics.registerFont(TTFont("Report", regular))
            pdfmetrics.registerFont(TTFont("Report-Bold", bold))
            return
    raise DocumentError("Для PDF-отчёта не найден шрифт с кириллицей")


class ReportCanvas(Canvas):
    """Добавляет ненавязчивый футер с фактическим общим количеством страниц."""

    def __init__(self, *args, header, **kwargs):
        """Сохраняет команды страниц до определения их общего числа."""
        super().__init__(*args, **kwargs)
        self.states = []
        self.header = header

    def showPage(self):
        """Откладывает запись текущей страницы без потери переносов таблицы."""
        self.states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        """Записывает страницы с итоговой нумерацией, используя встроенный шрифт."""
        total = len(self.states)
        for state in self.states:
            self.__dict__.update(state)
            self.header(self, None)
            self.setFont("Report", 7)
            self.setFillColor(MUTED)
            self.drawString(26, 23, "Сравнение документов")
            self.drawRightString(A4[0] - 26, 23, f"Страница {self._pageNumber} из {total}")
            super().showPage()
        super().save()


def render_report(report: dict) -> bytes:
    """Строит полный отчёт: длинные ячейки переносятся между страницами без обрезания."""
    _fonts()
    output = io.BytesIO()
    width = A4[0] - 52
    date = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M МСК")
    body = ParagraphStyle(
        "body",
        fontName="Report",
        fontSize=7.2,
        leading=10.4,
        textColor=INK,
        alignment=TA_LEFT,
        splitLongWords=True,
    )
    heading = ParagraphStyle(
        "heading", parent=body, fontName="Report-Bold", fontSize=18, leading=23, spaceAfter=15
    )
    section = ParagraphStyle(
        "section",
        parent=body,
        fontName="Report-Bold",
        fontSize=10,
        leading=14,
        spaceBefore=14,
        spaceAfter=8,
        keepWithNext=False,
    )

    def paragraph(value, style=body):
        """Экранирует пользовательский текст и сохраняет явные переносы строк."""
        return Paragraph(escape(str(value)).replace("\n", "<br/>").replace("\t", "    "), style)

    def header(canvas, document):
        """Рисует одинаковую шапку, отделённую от содержимого таблицы."""
        canvas.saveState()
        canvas.setFillColor(BLUE)
        canvas.setFont("Report-Bold", 11)
        canvas.drawString(26, A4[1] - 28, "Сравнение документов")
        canvas.setFont("Report", 7)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(A4[0] - 26, A4[1] - 28, date)
        canvas.setStrokeColor(LINE)
        canvas.line(26, A4[1] - 41, A4[0] - 26, A4[1] - 41)
        canvas.restoreState()

    stats = report["statistics"]
    modes = ["Алгоритмическое сравнение"]
    if report["ai_full"]:
        modes.append("ИИ: полная проверка документов")
    if report["ai_unverified"]:
        modes.append("ИИ: проверка непроверенных областей")
    if report["ai_full"] or report["ai_unverified"]:
        modes.append(
            "Статус ИИ: "
            + {
                "completed": "завершён",
                "partial": "частично выполнен",
                "failed": "ошибка",
                "not_requested": "не выполнялся",
                "running": "выполняется",
            }.get(report["ai_status"], report["ai_status"])
        )
    story = [paragraph("Отчёт по сравнению редакций", heading)]
    metadata = [
        ("Дата формирования:", date),
        ("Файл 1 (наша версия):", report["old_name"]),
        ("Файл 2 (контрагент):", report["new_name"]),
        ("Режимы анализа:", "\n".join(modes)),
        (
            "Итог:",
            f"Подтверждённых: {stats['total']}; непроверенных областей: {stats['unverified']}; замечаний ИИ: {stats['ai_needs_review']}",
        ),
    ]
    info = Table([[paragraph(a), paragraph(b)] for a, b in metadata], colWidths=[150, width - 150])
    info.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F5F9FE")),
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story += [info, Spacer(1, 9)]
    active = report["differences"]
    groups = (
        (
            "Подтверждённые различия",
            [d for d in active if d["status"] == "confirmed" and not is_unverified(d)],
        ),
        (
            "Замечания, требующие проверки",
            [d for d in active if d["status"] != "confirmed" and not is_unverified(d)],
        ),
        ("Непроверенные области", [d for d in active if is_unverified(d)]),
    )
    number = 0
    for title, items in groups:
        story.extend([CondPageBreak(100), paragraph(title, section)])
        if not items:
            story.append(paragraph("Отсутствуют"))
            continue
        rows = [
            [
                paragraph(value)
                for value in (
                    "№",
                    "Раздел / пункт",
                    "Тип",
                    "Статус / источник",
                    "Было (наша версия)",
                    "Стало (версия контрагента)",
                    "Страницы",
                )
            ]
        ]
        for item in items:
            number += 1
            source = (
                "Алгоритмы и ИИ"
                if item.get("verified_by") == "algorithms_and_ai"
                else "ИИ"
                if item.get("source") == "ai" or item.get("verified_by", "").startswith("ai")
                else "Алгоритмы"
            )
            if item.get("ai_mode"):
                source += (
                    " ("
                    + {"full": "полная проверка", "unverified": "адресная проверка"}.get(
                        item["ai_mode"], item["ai_mode"]
                    )
                    + ")"
                )
            status = {
                "confirmed": "Подтверждено",
                "needs_review": "Требует проверки",
                "unverified": "Не проверено",
                "ambiguous": "Неоднозначно",
            }.get(item["status"], item["status"])
            if is_unverified(item):
                status = (
                    "Неоднозначно"
                    if item["category"] == "ambiguous_match" or item["status"] == "ambiguous"
                    else "Не проверено"
                )
            pages = []
            for side, label in (("old", "Наша"), ("new", "Контр.")):
                values = sorted({region["page"] for region in item.get(side + "_regions", [])})
                pages.append(label + ": " + (", ".join(map(str, values)) or "нет привязки"))
            rows.append(
                [
                    paragraph(value)
                    for value in (
                        number,
                        item.get("old_section") or item.get("new_section") or item["title"],
                        TYPES.get(item["category"], item["category"]),
                        status + "\n" + source,
                        item.get("old_text") or "—",
                        item.get("new_text") or "—",
                        "\n".join(pages),
                    )
                ]
            )
        table = LongTable(
            rows,
            colWidths=[20, 60, 60, 73, 136, 136, width - 485],
            repeatRows=1,
            splitByRow=1,
            splitInRow=1,
            hAlign="LEFT",
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F0FB")),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#F7FAFE")],
                    ),
                    ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.append(table)
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=26,
        rightMargin=26,
        topMargin=59,
        bottomMargin=42,
        title="Отчёт по сравнению редакций",
        author="Сравнение документов",
    )
    document.build(story, canvasmaker=partial(ReportCanvas, header=header))
    return output.getvalue()
