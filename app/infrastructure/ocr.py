# app/infrastructure/ocr.py
"""Детерминированный Tesseract OCR с координатами и повторным чтением чисел.

Скан не считается пустым документом при отказе OCR. Подготовка изображения
сохраняет обратное преобразование, поэтому подсветка остаётся на исходном скане.
"""

import csv
import io
import math
import re
from collections.abc import Callable
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps
from scipy import ndimage

from app.domain.models import DocumentError
from app.infrastructure.processes import run_process
from app.observability import timed
from app.settings import Settings


def prepare_image(image: Image.Image) -> tuple[Image.Image, float]:
    """Удаляет мелкий шум и выравнивает небольшой наклон по проекции строк."""
    gray = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    small = gray.copy()
    small.thumbnail((750, 1000))
    mask = np.asarray(small) < 140
    best_angle, best_score = 0.0, 0.0
    for angle in np.arange(-2, 2.01, 0.5):
        rotated = ndimage.rotate(mask, angle, reshape=False, order=0)
        projection = rotated.sum(axis=1)
        score = float(np.var(projection))
        if score > best_score:
            best_angle, best_score = float(angle), score
    small.close()
    if best_angle:
        aligned = gray.rotate(best_angle, resample=Image.Resampling.BICUBIC, fillcolor=255)
        gray.close()
        gray = aligned
    result = gray.filter(ImageFilter.MedianFilter(3))
    gray.close()
    return result, best_angle


def original_box(
    box: tuple[float, float, float, float],
    angle: float,
    width: int,
    height: int,
    source_width: int | None = None,
    source_height: int | None = None,
) -> tuple[float, float, float, float]:
    """Обращает поворот OCR-копии для четырёх углов прямоугольника."""
    rad = math.radians(angle)
    source_width = source_width or width
    source_height = source_height or height
    points = []
    for x, y in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])):
        dx, dy = x - width / 2, y - height / 2
        points.append(
            (
                math.cos(rad) * dx - math.sin(rad) * dy + source_width / 2,
                math.sin(rad) * dx + math.cos(rad) * dy + source_height / 2,
            )
        )
    return (
        max(0, min(p[0] for p in points)),
        max(0, min(p[1] for p in points)),
        min(source_width, max(p[0] for p in points)),
        min(source_height, max(p[1] for p in points)),
    )


class TesseractOCR:
    """Ограниченный синхронный адаптер OCR, вызываемый только фоновым worker."""

    def __init__(self, settings: Settings):
        """Сохраняет языки, команду и порог уверенности из валидированных settings."""
        self.settings = settings

    def _read(
        self,
        image: Path,
        output: Path,
        psm: int,
        checkpoint: Callable[[str], None],
        language: str | None = None,
    ) -> list[dict]:
        """Читает TSV одного изображения; координаты пока заданы в его пикселях."""
        run_process(
            [
                self.settings.tesseract_command,
                str(image.resolve()),
                str(output.resolve()),
                "-l",
                language or self.settings.ocr_language,
                "--psm",
                str(psm),
                "tsv",
            ],
            output.parent,
            self.settings.process_timeout_seconds,
            checkpoint,
            "Распознавание OCR",
        )
        tsv = output.with_suffix(".tsv")
        if not tsv.exists() or tsv.stat().st_size > 10_000_000:
            raise DocumentError("OCR не вернул допустимые данные")
        rows = []
        for row in csv.DictReader(
            io.StringIO(tsv.read_text(encoding="utf-8")), delimiter="\t", quoting=csv.QUOTE_NONE
        ):
            if row.get("level") != "5" or not row.get("text", "").strip():
                continue
            x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
            if (
                row["text"].strip() == "%"
                and rows
                and re.fullmatch(r"\d+(?:[.,]\d+)*", rows[-1]["text"])
                and rows[-1]["line"]
                == (int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
            ):
                previous = rows[-1]
                previous["text"] += "%"
                previous["box"] = (
                    previous["box"][0],
                    min(y, previous["box"][1]),
                    x + w,
                    max(y + h, previous["box"][3]),
                )
                previous["confidence"] = min(
                    previous["confidence"], max(0, float(row["conf"]) / 100)
                )
                continue
            rows.append(
                {
                    "text": row["text"].strip(),
                    "box": (x, y, x + w, y + h),
                    "confidence": max(0, float(row["conf"]) / 100),
                    "line": (int(row["block_num"]), int(row["par_num"]), int(row["line_num"])),
                    "paragraph": (int(row["block_num"]), int(row["par_num"])),
                    "verified": True,
                }
            )
        return rows

    @timed("document_ocr")
    def recognize(
        self, image: Image.Image, directory: Path, checkpoint: Callable[[str], None]
    ) -> list[dict]:
        """Распознаёт страницу и независимо перечитывает спорные числовые участки."""
        directory.mkdir(parents=True, exist_ok=True)
        prepared, angle = prepare_image(image)
        orientation = 0
        file = directory / "prepared.png"
        prepared.save(file)
        try:
            rows = self._read(file, directory / "layout", 3, checkpoint)
            if not rows:
                rows = self._read(file, directory / "sparse", 11, checkpoint)
            mean_quality = sum(r["confidence"] for r in rows) / max(1, len(rows))
            if (len(rows) < 5 or mean_quality < 0.55) and float(
                (np.asarray(prepared) < 155).mean()
            ) > 0.001:
                best_score = sum(len(r["text"]) * r["confidence"] ** 2 for r in rows)
                for rotation in (90, 180, 270):
                    rotated = image.rotate(rotation, expand=True, fillcolor="white")
                    candidate, candidate_angle = prepare_image(rotated)
                    rotated.close()
                    rotated_file = directory / f"orientation-{rotation}.png"
                    candidate.save(rotated_file)
                    rotated_rows = self._read(
                        rotated_file, directory / f"orientation-{rotation}", 3, checkpoint
                    )
                    score = sum(len(r["text"]) * r["confidence"] ** 2 for r in rotated_rows)
                    if score > best_score:
                        prepared.close()
                        prepared, rows, angle, orientation, best_score = (
                            candidate,
                            rotated_rows,
                            candidate_angle,
                            rotation,
                            score,
                        )
                    else:
                        candidate.close()
                prepared.save(file)
            if rows:
                letters = "".join(r["text"] for r in rows)
                russian = sum("А" <= c <= "я" or c in "Ёё" for c in letters)
                language = "rus" if russian > len(letters) * 0.15 else "eng"
                try:
                    alternate = self._read(file, directory / "alternate", 6, checkpoint, language)
                    grid = {}
                    for word in alternate:
                        x0, y0, x1, y1 = word["box"]
                        key = (int((x0 + x1) / 128), int((y0 + y1) / 128))
                        grid.setdefault(key, []).append(word)
                    for row in rows:
                        x0, y0, x1, y1 = row["box"]
                        nearby = []
                        for gx in range(max(0, int(x0 / 64) - 1), int(x1 / 64) + 2):
                            for gy in range(max(0, int(y0 / 64) - 1), int(y1 / 64) + 2):
                                nearby.extend(grid.get((gx, gy), []))
                        overlapping = [
                            word
                            for word in nearby
                            if x0 - 3 <= (word["box"][0] + word["box"][2]) / 2 <= x1 + 3
                            and y0 - 3 <= (word["box"][1] + word["box"][3]) / 2 <= y1 + 3
                        ]
                        actual = "".join(
                            word["text"]
                            for word in sorted(overlapping, key=lambda word: word["box"][0])
                        )
                        row["verified"] = bool(
                            actual and actual.replace(" ", "") == row["text"].replace(" ", "")
                        )
                        if not row["verified"]:
                            row["confidence"] = min(row["confidence"], 0.64)
                except DocumentError:
                    for row in rows:
                        row["verified"] = False
                        row["confidence"] = min(row["confidence"], 0.64)
            # Повторное OCR локального числа другой конфигурацией; значения не исправляются по другой редакции.
            numeric = [
                r
                for r in rows
                if re.search(r"\d", r["text"])
                and (
                    len(re.sub(r"\D", "", r["text"])) >= 8
                    or "%" in r["text"]
                    or r["confidence"] < 0.85
                )
            ]
            for row in numeric[:200]:
                checkpoint("Проверка чисел OCR")
                x0, y0, x1, y1 = row["box"]
                crop = prepared.crop(
                    (
                        max(0, x0 - 8),
                        max(0, y0 - 5),
                        min(prepared.width, x1 + 8),
                        min(prepared.height, y1 + 5),
                    )
                )
                crop = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS)
                crop_path = directory / "numeric.png"
                crop.save(crop_path)
                crop.close()
                try:
                    independent = self._read(crop_path, directory / "numeric", 7, checkpoint, "eng")
                except DocumentError:
                    row["verified"] = False
                    row["confidence"] = min(row["confidence"], 0.64)
                    continue
                expected = re.sub(r"[^\d.,%]", "", row["text"])
                actual = re.sub(r"[^\d.,%]", "", "".join(r["text"] for r in independent))
                row["verified"] = bool(expected and expected == actual)
                if not row["verified"]:
                    row["confidence"] = min(row["confidence"], 0.64)
            for row in rows:
                box = original_box(row["box"], angle, prepared.width, prepared.height)
                row["box"] = original_box(
                    box, orientation, prepared.width, prepared.height, image.width, image.height
                )
            return rows
        finally:
            prepared.close()
