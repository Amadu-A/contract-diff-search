# app/infrastructure/visual_compare.py
"""Измеряет локальное сходство графики без принятия бизнес-решения о равенстве."""

from PIL import Image

from app.domain.models import DocumentVersion
from app.infrastructure.graphics import ssim


class ImageComparer:
    """Проверяет pHash-кандидатов SSIM после приведения к единому масштабу."""

    def measure(self, old: DocumentVersion, new: DocumentVersion) -> dict[tuple[str, str], float]:
        """Возвращает ограниченный набор измерений; высокий SSIM не скрывает локальную правку."""
        result = {}
        for a in old.graphics:
            candidates = sorted(
                new.graphics,
                key=lambda b: (int(a.perceptual_hash, 16) ^ int(b.perceptual_hash, 16)).bit_count(),
            )[:8]
            for b in candidates:
                if (int(a.perceptual_hash, 16) ^ int(b.perceptual_hash, 16)).bit_count() <= 16:
                    with Image.open(a.image) as first, Image.open(b.image) as second:
                        result[(a.id, b.id)] = ssim(first, second)
        return result
