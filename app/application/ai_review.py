# app/application/ai_review.py
"""Ограниченная оркестрация двух ИИ-проходов с checkpoint, повторами и сохранением частичного результата."""

import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from app.application.ai_evidence import merge_reply
from app.application.ai_planning import full_plan, unit_id, unverified_plan
from app.application.ports import GeometryRefiner, ModelReviewer
from app.domain.models import AIModes, AIObservation, Cancelled, VerificationError
from app.domain.text import normalize
from app.domain.visibility import uncertainty_ids

PARTIAL_WARNING = (
    "ИИ-проверка выполнена не полностью; успешные операции и алгоритмический результат сохранены"
)


@dataclass(frozen=True)
class AILimits:
    """Лимиты приложения; размер пакета ограничивает очередь ожидания, а не весь документ."""

    batch_size: int = 12
    concurrency: int = 1
    retries: int = 2
    total_seconds: int = 2400
    retry_delay: float = 0.5
    context_chars: int = 3600
    region_limit: int = 6


def public_progress(state) -> dict:
    """Исключает тексты, цитаты, изображения и файловые пути из текущего прогресса API."""
    return {
        "full": asdict(state.full),
        "unverified": asdict(state.unverified),
        "elapsed_ms": state.elapsed_ms,
        "original_unverified": len(state.original_uncertainties),
        "resolved_ai": len(state.resolutions),
    }


@contextmanager
def _pool(concurrency):
    """При отмене снимает ещё не запущенные запросы до ожидания ограниченного числа активных."""
    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="vlm")
    try:
        yield executor
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


class AIReviewCoordinator:
    """Выполняет каждую запланированную единицу и сохраняет независимые результаты режимов."""

    def __init__(
        self, reviewer: ModelReviewer, limits: AILimits, refiner: GeometryRefiner | None = None
    ):
        """Получает порт одиночного запроса и значения лимитов из composition root."""
        self.reviewer, self.limits = reviewer, limits
        self.refiner = refiner

    def _call(self, result, unit, deadline, job_id, batch):
        """Обрабатывает временные отказы одной единицы без остановки остальных."""
        started = time.monotonic()
        unit.job_id = job_id
        for attempt in range(self.limits.retries + 1):
            if time.monotonic() >= deadline:
                return None, VerificationError("time_budget"), (time.monotonic() - started) * 1000
            unit.attempts += 1
            try:
                reply = self.reviewer.review(unit, result.old, result.new, attempt)
                return reply, None, (time.monotonic() - started) * 1000
            except Exception as exc:
                error = exc if isinstance(exc, VerificationError) else VerificationError("internal")
                retry = (
                    error.retryable
                    and attempt < self.limits.retries
                    and time.monotonic() < deadline
                )
                unit.error_kind = error.kind
                feature = {
                    "unsupported_parameter": "thinking_flag",
                    "unsupported_schema": "json_schema",
                }.get(error.kind)
                if feature and feature not in unit.disabled_features:
                    unit.disabled_features.append(feature)
                logging.getLogger(__name__).warning(
                    "ai_unit_failed",
                    extra={
                        "job_id": job_id,
                        "unit_id": unit.id,
                        "ai_mode": unit.mode,
                        "batch": batch,
                        "stage": "request",
                        "attempt": unit.attempts,
                        "exception_class": type(exc).__name__,
                        "error_kind": error.kind,
                        "http_status": error.http_status,
                        "timed_out": error.kind == "timeout",
                        "retrying": retry,
                        "duration_ms": round((time.monotonic() - started) * 1000, 2),
                        "json_status": "invalid"
                        if error.kind
                        in {"json_validation", "invalid_json", "empty_content", "output_truncated"}
                        else "not_validated",
                    },
                )
                if not retry:
                    return None, error, (time.monotonic() - started) * 1000
                time.sleep(
                    min(self.limits.retry_delay * 2**attempt, max(0, deadline - time.monotonic()))
                )
        return None, VerificationError("internal"), (time.monotonic() - started) * 1000

    def _progress(self, result, mode, units, final=False):
        """Вычисляет счётчики только из сохранённых состояний единиц и реального покрытия страниц."""
        progress = getattr(result.ai_review, mode)
        progress.planned = len(units)
        progress.succeeded = sum(u.status == "completed" for u in units)
        progress.failed = sum(u.status == "failed" for u in units)
        progress.processed = progress.succeeded + progress.failed
        progress.remaining = progress.planned - progress.processed
        progress.error_counts = dict(Counter(u.error_kind for u in units if u.error_kind))
        progress.pages_planned, progress.pages_processed = {}, {}
        for side in ("old", "new"):
            pages = sorted({r.page for u in units for r in getattr(u, side + "_regions")})
            progress.pages_planned[side] = pages
            progress.pages_processed[side] = [
                p
                for p in pages
                if all(
                    u.status == "completed"
                    for u in units
                    if any(r.page == p for r in getattr(u, side + "_regions"))
                )
            ]
        if final:
            progress.status = (
                "completed"
                if progress.succeeded == progress.planned
                else "partial"
                if progress.succeeded
                else "failed"
            )
        else:
            progress.status = "running"

    def _same_evidence(self, result, unit, reply):
        """Подтверждает целый совпавший блок только при полном независимом текстовом доказательстве."""
        if (
            reply.outcome != "same"
            or not reply.readable
            or not reply.complete
            or len(unit.old_elements) != 1
            or len(unit.new_elements) != 1
        ):
            return
        a = next(e for e in result.old.elements if e.id == unit.old_elements[0])
        b = next(e for e in result.new.elements if e.id == unit.new_elements[0])
        if (
            normalize(a.text).text != normalize(b.text).text
            or unit.old_text != a.text
            or unit.new_text != b.text
        ):
            return
        if (
            not a.words
            or not b.words
            or any(float(e.attributes.get("geometry_coverage", "1")) < 0.98 for e in (a, b))
            or not all(w.verified and w.confidence >= 0.65 for w in [*a.words, *b.words])
        ):
            return
        reply.evidence.append(
            AIObservation(
                "unchanged",
                a.text,
                b.text,
                "o0",
                "n0",
                "Совпадение подтверждено независимым извлечением и визуальной проверкой",
                1,
            )
        )

    def verify(self, result, checkpoint, *, modes: AIModes | None = None, job_id="", persist=None):
        """Обходит все пакеты; последующая ошибка не удаляет уже обработанные единицы."""
        modes = modes or AIModes(full=True)
        persist = persist or (lambda current: None)
        started = time.monotonic()
        deadline = started + self.limits.total_seconds
        state = result.ai_review
        if not state.original_uncertainties:
            state.original_uncertainties = sorted(uncertainty_ids(result.differences))
        result.ai_status = "running"
        result.warnings = [warning for warning in result.warnings if warning != PARTIAL_WARNING]
        for name, enabled in (("full", modes.full), ("unverified", modes.unverified)):
            if enabled and getattr(state, name).status == "not_requested":
                getattr(state, name).status = "queued"
        if self.refiner is not None and (modes.full or modes.unverified):
            try:
                self.refiner.refine(result, checkpoint)
            except Cancelled:
                raise
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "ai_geometry_refine_failed",
                    extra={
                        "job_id": job_id,
                        "phase": "geometry",
                        "exception_class": type(exc).__name__,
                        "error_kind": "geometry_refinement",
                    },
                )
            persist(result)
        for mode, enabled, planner in (
            ("full", modes.full, full_plan),
            ("unverified", modes.unverified, unverified_plan),
        ):
            progress = getattr(state, mode)
            if not enabled:
                progress.status = "not_requested"
                continue
            progress.status = "queued"
            if mode == "unverified":
                progress.reused = len(
                    set(state.original_uncertainties) - uncertainty_ids(result.differences)
                )
            plan = planner(result, self.limits.context_chars, self.limits.region_limit)
            saved = {u.id: u for u in state.units}
            units = [saved.get(u.id, u) for u in plan]
            identifiers = {u.id for u in units}
            units.extend(
                u
                for u in state.units
                if u.mode == mode and u.status == "completed" and u.id not in identifiers
            )
            state.units = [u for u in state.units if u.mode != mode] + units
            self._progress(result, mode, units)
            persist(result)
            pending = [u for u in units if u.status != "completed"]
            with _pool(self.limits.concurrency) as pool:
                offset = 0
                while offset < len(pending):
                    batch = offset // self.limits.batch_size + 1
                    checkpoint(
                        f"ИИ: {'все документы' if mode == 'full' else 'непроверенные области'}, пакет {batch}"
                    )
                    if time.monotonic() >= deadline:
                        for unit in pending[offset:]:
                            unit.status, unit.error_kind = "skipped", "time_budget"
                        break
                    group = pending[offset : offset + self.limits.batch_size]
                    for unit in group:
                        unit.status = "running"
                    futures = {
                        pool.submit(self._call, result, u, deadline, job_id, batch): u
                        for u in group
                    }
                    for future in as_completed(futures):
                        unit = futures[future]
                        reply, error, duration = future.result()
                        unit.duration_ms = round(duration, 2)
                        if reply is not None:
                            unit.readable = reply.readable
                            self._same_evidence(result, unit, reply)
                            merge_reply(result, unit, reply)
                            unit.observations = [*reply.findings, *reply.evidence]
                            unit.outcome = reply.outcome
                            unit.status = (
                                "completed" if reply.complete and reply.readable else "failed"
                            )
                            unit.error_kind = (
                                "unreadable"
                                if not reply.readable
                                else ""
                                if reply.complete
                                else "context_incomplete"
                            )
                        else:
                            unit.status = "skipped" if error.kind == "time_budget" else "failed"
                            unit.error_kind = error.kind
                        if unit.status != "completed":
                            for difference in result.differences:
                                if (
                                    difference.active
                                    and (difference.uncertainty_id or difference.id) in unit.targets
                                    and difference.unverified_reason != "unsupported_property"
                                ):
                                    difference.unverified_reason = (
                                        "unreadable_image"
                                        if unit.error_kind == "unreadable"
                                        else "partial_coverage"
                                        if unit.error_kind == "context_incomplete"
                                        else "model_error"
                                    )
                        self._progress(result, mode, units)
                        state.elapsed_ms = round((time.monotonic() - started) * 1000, 2)
                        persist(result)
                        logging.getLogger(__name__).info(
                            "ai_unit_checkpoint",
                            extra={
                                "job_id": job_id,
                                "ai_mode": mode,
                                "unit_id": unit.id,
                                "batch": batch,
                                "status": unit.status,
                                "planned": progress.planned,
                                "completed": progress.processed,
                                "remaining": progress.remaining,
                                "duration_ms": unit.duration_ms,
                                "error_kind": unit.error_kind,
                                "json_status": "valid" if reply else "not_validated",
                            },
                        )
                        checkpoint(
                            f"ИИ: {mode}, обработано {progress.processed}/{progress.planned}"
                        )
                    offset += len(group)
                    if time.monotonic() < deadline and any(
                        d.active and d.category == "ai_contradiction" for d in result.differences
                    ):
                        known = {u.id for u in units}
                        for followup in unverified_plan(
                            result, self.limits.context_chars, self.limits.region_limit
                        ):
                            if followup.kind != "conflict":
                                continue
                            followup.mode = mode
                            followup.id = unit_id(followup)
                            if followup.id not in known:
                                units.append(followup)
                                pending.append(followup)
                                known.add(followup.id)
            self._progress(result, mode, units, final=True)
            persist(result)
        requested = [
            getattr(state, mode)
            for mode, enabled in (("full", modes.full), ("unverified", modes.unverified))
            if enabled
        ]
        result.ai_status = (
            "completed"
            if all(p.status == "completed" for p in requested)
            else "partial"
            if any(p.succeeded for p in requested)
            else "failed"
        )
        state.elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        if result.ai_status != "completed":
            result.warnings.append(PARTIAL_WARNING)
        persist(result)
