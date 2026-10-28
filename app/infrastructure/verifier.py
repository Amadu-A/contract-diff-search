# app/infrastructure/verifier.py
"""Один ограниченный мультимодальный запрос к shared-vlm с безопасной диагностикой."""

import base64
import io
import json
import logging
import time
from pathlib import Path
from typing import Literal

import httpx
from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.models import (
    AIObservation,
    AIReply,
    ReviewUnit,
    VerificationError,
    VisualRegion,
    Word,
)
from app.infrastructure.pdf import render_page
from app.infrastructure.prompts import system_prompt
from app.settings import Settings

CATEGORIES = Literal[
    "unchanged",
    "text_modified",
    "number_modified",
    "text_added",
    "text_deleted",
    "text_moved",
    "numbering_changed",
    "formatting_changed",
    "structure_changed",
    "table_modified",
    "table_structure_changed",
    "graphic_added",
    "graphic_deleted",
    "graphic_modified",
    "unverified_region",
]


class FindingSchema(BaseModel):
    """Разрешённые значения и ссылки; произвольные координаты и команды отклоняются."""

    model_config = ConfigDict(extra="forbid")
    category: CATEGORIES
    old_value: str = Field("", max_length=6000)
    new_value: str = Field("", max_length=6000)
    old_quote: str = Field("", max_length=6000)
    new_quote: str = Field("", max_length=6000)
    old_ref: str = Field("", pattern=r"^(?:o\d+)?$")
    new_ref: str = Field("", pattern=r"^(?:n\d+)?$")
    reason: str = Field("", max_length=1000)
    confidence: float = Field(0, ge=0, le=1)
    mapping_confirmed: bool = False
    mapping_reason: str = Field("", max_length=1000)
    covers_region: bool = False
    absence_confirmed: bool = False


class ResponseSchema(BaseModel):
    """Проверяемый JSON со статусом читаемости и честной полнотой одной единицы."""

    model_config = ConfigDict(extra="forbid")
    outcome: Literal["same", "different", "uncertain"]
    readable: bool
    complete: bool = True
    findings: list[FindingSchema] = Field(default_factory=list, max_length=30)
    evidence: list[FindingSchema] = Field(default_factory=list, max_length=10)


class SharedVlmVerifier:
    """Адаптер одиночной операции; планирование, повторы и прогресс принадлежат application."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None, ocr=None):
        """Принимает логический endpoint, подменяемый HTTP transport и независимое OCR."""
        self.settings, self.transport, self.ocr = settings, transport, ocr

    def _visuals(self, unit, version, side, budget):
        """Рендерит локальные области и объединяет их в один подписанный лист на сторону."""
        regions = getattr(unit, side + "_regions")
        pieces, evidence, cache = [], [], {}
        prefix = "o" if side == "old" else "n"
        try:
            for index, region in enumerate(regions):
                page = version.pages[region.page - 1]
                if region.page not in cache:
                    if len(cache) >= 2:
                        _, expired = cache.popitem()
                        expired.close()
                    cache[region.page] = render_page(
                        Path(version.pdf), region.page, 3.0, self.settings.max_page_pixels
                    )
                image = cache[region.page]
                x0, y0 = max(0, region.x0 - 3), max(0, region.y0 - 3)
                x1, y1 = min(page.width, region.x1 + 3), min(page.height, region.y1 + 3)
                if x1 <= x0 or y1 <= y0:
                    continue
                pixel_box = (
                    int(x0 * image.width / page.width),
                    int(y0 * image.height / page.height),
                    int(x1 * image.width / page.width),
                    int(y1 * image.height / page.height),
                )
                crop = image.crop(pixel_box)
                x0, y0 = (
                    pixel_box[0] * page.width / image.width,
                    pixel_box[1] * page.height / image.height,
                )
                if unit.mode == "unverified" and self.ocr is not None:
                    directory = (
                        Path(version.pdf).parent.parent / "ai-ocr" / unit.id / f"{side}-{index}"
                    )
                    try:
                        rows = self.ocr.recognize(crop, directory, lambda stage: None)
                        for row in rows:
                            box = row["box"]
                            evidence.append(
                                Word(
                                    row["text"],
                                    0,
                                    len(row["text"]),
                                    VisualRegion(
                                        region.page,
                                        x0 + box[0] * page.width / image.width,
                                        y0 + box[1] * page.height / image.height,
                                        x0 + box[2] * page.width / image.width,
                                        y0 + box[3] * page.height / image.height,
                                    ),
                                    row["confidence"],
                                    row["verified"],
                                )
                            )
                    except Exception as exc:
                        logging.getLogger(__name__).warning(
                            "ai_evidence_refine_failed",
                            extra={
                                "unit_id": unit.id,
                                "job_id": unit.job_id,
                                "ai_mode": unit.mode,
                                "exception_class": type(exc).__name__,
                                "error_kind": "ocr_refinement",
                            },
                        )
                crop.thumbnail((1600, 2400))
                labeled = Image.new("RGB", (crop.width, crop.height + 24), "white")
                ImageDraw.Draw(labeled).text(
                    (4, 4), f"{prefix}{index} / page {region.page}", fill="black"
                )
                labeled.paste(crop, (0, 24))
                crop.close()
                pieces.append(labeled)
            if not pieces:
                return None, evidence
            width, height = max(p.width for p in pieces), sum(p.height for p in pieces)
            sheet = Image.new("RGB", (width, height), "white")
            offset = 0
            for piece in pieces:
                sheet.paste(piece, (0, offset))
                offset += piece.height
            factor = min(1, (budget / max(1, width * height)) ** 0.5)
            if factor < 1:
                resized = sheet.resize((max(1, int(width * factor)), max(1, int(height * factor))))
                sheet.close()
                sheet = resized
            buffer = io.BytesIO()
            sheet.save(buffer, format="PNG")
            sheet.close()
            return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(), evidence
        finally:
            for image in cache.values():
                image.close()
            for piece in pieces:
                piece.close()

    def review(self, unit: ReviewUnit, old, new, attempt: int = 0) -> AIReply:
        """Возвращает структурированный ответ; ни payload, ни содержимое ошибки не пишется в лог."""
        budget = self.settings.vlm_image_pixels
        if unit.error_kind == "context_limit":
            budget = max(200000, budget // (2**attempt))
        payload = {
            "task_id": unit.id,
            "mode": unit.mode,
            "task": unit.kind,
            "old": unit.old_text,
            "new": unit.new_text,
            "old_sources": [
                {"ref": f"o{i}", "page": r.page} for i, r in enumerate(unit.old_regions)
            ],
            "new_sources": [
                {"ref": f"n{i}", "page": r.page} for i, r in enumerate(unit.new_regions)
            ],
            "old_elements": unit.old_elements,
            "new_elements": unit.new_elements,
            **{
                side + "_structure": [
                    {
                        "id": e.id,
                        "section": e.section,
                        "kind": e.kind,
                        "column": e.attributes.get("column"),
                        "party": e.attributes.get("party"),
                    }
                    for e in version.elements
                    if e.id in getattr(unit, side + "_elements")
                ]
                for side, version in (("old", old), ("new", new))
            },
        }
        images, observations, visual_refs = [], {}, []
        for side, version in (("old", old), ("new", new)):
            visual, extra = self._visuals(unit, version, side, budget)
            observations[side] = extra
            if extra:
                payload[side + "_independent_ocr"] = " ".join(w.text for w in extra)[
                    : self.settings.vlm_context_chars
                ]
            if visual:
                prefix = "o" if side == "old" else "n"
                visual_refs.extend(
                    f"{prefix}{index}" for index in range(len(getattr(unit, side + "_regions")))
                )
                images.append({"type": "image_url", "image_url": {"url": visual}})
        request = {
            "model": self.settings.shared_vlm_model,
            "temperature": 0,
            "max_tokens": self.settings.vlm_max_retry_tokens
            if attempt
            else self.settings.vlm_max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt(unit.mode, unit.kind)},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
                        *images,
                    ],
                },
            ],
        }
        if (
            self.settings.vlm_json_schema
            and unit.error_kind != "unsupported_schema"
            and "json_schema" not in unit.disabled_features
        ):
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "document-review",
                    "schema": ResponseSchema.model_json_schema(),
                },
            }
        if (
            self.settings.vlm_disable_thinking
            and unit.error_kind != "unsupported_parameter"
            and "thinking_flag" not in unit.disabled_features
        ):
            request["chat_template_kwargs"] = {"enable_thinking": False}
        started = time.monotonic()
        try:
            with httpx.Client(
                timeout=self.settings.vlm_timeout_seconds, transport=self.transport, trust_env=False
            ) as client:
                response = client.post(
                    self.settings.shared_vlm_base_url.rstrip("/") + "/chat/completions",
                    json=request,
                )
            if response.status_code >= 400:
                raw = response.text.lower()
                kind = (
                    "context_limit"
                    if any(t in raw for t in ("context length", "maximum context", "max_model_len"))
                    else "unsupported_schema"
                    if "json_schema" in raw or "structured output" in raw
                    else "unsupported_parameter"
                    if any(
                        t in raw for t in ("unsupported", "not permitted", "chat_template_kwargs")
                    )
                    else "http_error"
                )
                raise VerificationError(
                    kind,
                    http_status=response.status_code,
                    retryable=response.status_code in {408, 429, 500, 502, 503, 504}
                    or kind in {"context_limit", "unsupported_parameter", "unsupported_schema"},
                )
            if len(response.content) > 150000:
                raise VerificationError("response_too_large")
            envelope = response.json()
            choice = envelope["choices"][0]
            text = choice["message"].get("content")
            usage = envelope.get("usage", {})
            logging.getLogger(__name__).info(
                "ai_response_received",
                extra={
                    "unit_id": unit.id,
                    "job_id": unit.job_id,
                    "attempt": unit.attempts,
                    "ai_mode": unit.mode,
                    "http_status": response.status_code,
                    "finish_reason": choice.get("finish_reason"),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )
            if choice.get("finish_reason") == "length":
                raise VerificationError("output_truncated", retryable=True)
            if not isinstance(text, str) or not text.strip():
                raise VerificationError("empty_content", retryable=True)
            parsed = ResponseSchema.model_validate_json(text)
            unchanged = [finding for finding in parsed.findings if finding.category == "unchanged"]
            parsed.findings = [
                finding for finding in parsed.findings if finding.category != "unchanged"
            ]
            parsed.evidence.extend(unchanged)
            if parsed.outcome == "different" and not parsed.findings:
                raise VerificationError("invalid_semantic_response", retryable=True)
            if parsed.outcome == "same" and parsed.findings:
                raise VerificationError("invalid_semantic_response", retryable=True)
            return AIReply(
                parsed.outcome,
                parsed.readable,
                [AIObservation(**finding.model_dump()) for finding in parsed.findings],
                [AIObservation(**finding.model_dump()) for finding in parsed.evidence],
                parsed.complete,
                observations["old"],
                observations["new"],
                visual_refs,
            )
        except VerificationError:
            raise
        except httpx.TimeoutException:
            raise VerificationError("timeout", retryable=True) from None
        except httpx.TransportError:
            raise VerificationError("network", retryable=True) from None
        except ValidationError as exc:
            logging.getLogger(__name__).warning(
                "ai_json_validation_failed",
                extra={
                    "job_id": unit.job_id,
                    "unit_id": unit.id,
                    "ai_mode": unit.mode,
                    "attempt": unit.attempts,
                    "json_status": ",".join(
                        sorted(
                            {
                                error["type"]
                                for error in exc.errors(include_input=False, include_context=False)
                            }
                        )
                    ),
                },
            )
            raise VerificationError("json_validation", retryable=True) from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise VerificationError("invalid_json", retryable=True) from None
