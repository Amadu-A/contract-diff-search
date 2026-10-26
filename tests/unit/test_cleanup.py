# tests/unit/test_cleanup.py
"""Уборка аварийных профилей ограничена корнем и не затрагивает активные файлы."""

import os
import time

from app.infrastructure.storage import FileStorage


def test_stale_office_profiles_removed_without_touching_active_or_unrelated(settings):
    """Просроченный lo-профиль удаляется, свежий профиль и посторонний каталог сохраняются."""
    root = settings.data_dir / "processes"
    settings.process_temp_dir = root
    root.mkdir(parents=True)
    for name in ("lo-stale", "lo-active", "unrelated"):
        (root / name).mkdir()
    old = time.time() - 7200
    os.utime(root / "lo-stale", (old, old))
    os.utime(root / "unrelated", (old, old))
    FileStorage(settings).cleanup_transients(time.time() - 3600)
    assert not (root / "lo-stale").exists()
    assert (root / "lo-active").exists() and (root / "unrelated").exists()
