# app/runtime.py
"""Применение Pydantic-лимитов до загрузки библиотек с нативными пулами потоков."""

import os

from app.settings import Settings


def configure_native_threads(settings: Settings) -> None:
    """Ограничивает BLAS/OpenMP единой верхней границей до первого импорта NumPy."""
    for name in (
        "openblas_num_threads",
        "omp_num_threads",
        "mkl_num_threads",
        "numexpr_num_threads",
        "blis_num_threads",
        "veclib_maximum_threads",
        "omp_thread_limit",
    ):
        os.environ[name.upper()] = str(min(settings.native_threads, getattr(settings, name)))
