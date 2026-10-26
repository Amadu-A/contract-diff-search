# app/infrastructure/graphics.py
"""Локальная графика: цветные печати, pHash, геометрия и SSIM после масштаба."""

import hashlib
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage
from scipy.fft import dctn

from app.domain.models import GraphicElement, VisualRegion


def fingerprint(image: Image.Image) -> tuple[str, str]:
    """Даёт точный пиксельный SHA256 и DCT pHash для поиска кандидатов."""
    normalized = image.convert("RGB")
    digest = hashlib.sha256(normalized.tobytes() + str(normalized.size).encode()).hexdigest()
    thumb = image.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
    spectrum = dctn(np.asarray(thumb, dtype=float), norm="ortho")[:8, :8]
    median = np.median(spectrum.ravel()[1:])
    bits = spectrum > median
    value = sum(int(b) << i for i, b in enumerate(bits.ravel()))
    normalized.close()
    thumb.close()
    return digest, f"{value:016x}"


def ssim(first: Image.Image, second: Image.Image) -> float:
    """Сравнивает выровненные по масштабу локальные области, сохраняя чувствительность к мелким деталям."""
    a = np.asarray(first.convert("L").resize((256, 256)), dtype=float)
    b = np.asarray(second.convert("L").resize((256, 256)), dtype=float)
    ma, mb = ndimage.gaussian_filter(a, 1.5), ndimage.gaussian_filter(b, 1.5)
    va = ndimage.gaussian_filter(a * a, 1.5) - ma * ma
    vb = ndimage.gaussian_filter(b * b, 1.5) - mb * mb
    cov = ndimage.gaussian_filter(a * b, 1.5) - ma * mb
    value = ((2 * ma * mb + 6.5025) * (2 * cov + 58.5225)) / (
        (ma * ma + mb * mb + 6.5025) * (va + vb + 58.5225)
    )
    return float(value.mean())


def extract_blue_graphics(
    image: Image.Image, page: int, width: float, height: float, output: Path
) -> list[GraphicElement]:
    """Выделяет локальные цветные объекты скана; целая страница не считается печатью."""
    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    mask = (
        (rgb[:, :, 2] > rgb[:, :, 0] + 22)
        & (rgb[:, :, 2] > rgb[:, :, 1] + 10)
        & (rgb[:, :, 0] < 225)
    )
    joined = ndimage.binary_dilation(mask, iterations=max(2, int(image.width / 200)))
    labels, _ = ndimage.label(joined)
    result = []
    for slices in ndimage.find_objects(labels):
        if slices is None:
            continue
        ys, xs = slices
        if (xs.stop - xs.start) < image.width * 0.025 or (
            ys.stop - ys.start
        ) < image.height * 0.015:
            continue
        if int(mask[slices].sum()) < 80:
            continue
        crop = image.crop((xs.start, ys.start, xs.stop, ys.stop))
        file = output / f"graphic-{page}-{len(result)}.png"
        crop.save(file)
        digest, phash = fingerprint(crop)
        crop.close()
        result.append(
            GraphicElement(
                f"G{page}-{len(result)}",
                VisualRegion(
                    page,
                    xs.start * width / image.width,
                    ys.start * height / image.height,
                    xs.stop * width / image.width,
                    ys.stop * height / image.height,
                ),
                digest,
                phash,
                str(file),
                "Цветная печать / рукописная подпись; содержимое требует визуальной проверки",
            )
        )
    return result


def extract_unknown_graphics(
    image: Image.Image,
    words: list[dict],
    known: list[GraphicElement],
    page: int,
    width: float,
    height: float,
    output: Path,
) -> list[GraphicElement]:
    """Учитывает чёрную графику и рукопись вне распознанных слов как непроверенные объекты."""
    reduced = image.convert("L")
    reduced.thumbnail((1000, 1400))
    mask = np.asarray(reduced) < 150
    scale_x, scale_y = reduced.width / width, reduced.height / height
    for box in [w["box"] for w in words] + [
        (g.region.x0, g.region.y0, g.region.x1, g.region.y1) for g in known
    ]:
        x0, y0, x1, y1 = box
        mask[
            max(0, int(y0 * scale_y) - 2) : min(mask.shape[0], int(y1 * scale_y) + 3),
            max(0, int(x0 * scale_x) - 2) : min(mask.shape[1], int(x1 * scale_x) + 3),
        ] = False
    border = max(2, int(reduced.width * 0.025))
    mask[:border] = False
    mask[-border:] = False
    mask[:, :border] = False
    mask[:, -border:] = False
    horizontal = ndimage.binary_opening(mask, structure=np.ones((1, max(20, reduced.width // 10))))
    vertical = ndimage.binary_opening(mask, structure=np.ones((max(20, reduced.height // 10), 1)))
    mask &= ~(horizontal | vertical)
    components, _ = ndimage.label(mask)
    sizes = np.bincount(components.ravel())
    mask &= sizes[components] >= 8
    clusters, _ = ndimage.label(ndimage.binary_dilation(mask, iterations=5))
    result = []
    for area in ndimage.find_objects(clusters):
        if area is None:
            continue
        ys, xs = area
        if int(mask[area].sum()) < 70 or xs.stop - xs.start < 20 or ys.stop - ys.start < 12:
            continue
        if (xs.stop - xs.start) * (ys.stop - ys.start) > reduced.width * reduced.height * 0.25:
            continue
        region = VisualRegion(
            page, xs.start / scale_x, ys.start / scale_y, xs.stop / scale_x, ys.stop / scale_y
        )
        crop = image.crop(
            (
                int(region.x0 * image.width / width),
                int(region.y0 * image.height / height),
                int(region.x1 * image.width / width),
                int(region.y1 * image.height / height),
            )
        )
        file = output / f"unknown-{page}-{len(result)}.png"
        crop.save(file)
        digest, phash = fingerprint(crop)
        crop.close()
        result.append(
            GraphicElement(
                f"U{page}-{len(result)}",
                region,
                digest,
                phash,
                str(file),
                "Нераспознанная графика, рукопись или текст: требуется ручная проверка",
                0.5,
            )
        )
    reduced.close()
    return result
