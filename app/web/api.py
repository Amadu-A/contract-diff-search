# app/web/api.py
"""Тонкий HTTP transport: сессионный доступ, загрузка и выдача временных результатов."""

import io
import secrets
import shutil
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app.application import queries
from app.application.service import ComparisonService
from app.bootstrap import build_service
from app.domain.models import AccessDenied, DocumentError
from app.infrastructure.pdf import render_page
from app.settings import ROOT, Settings
from app.web.body_limit import BodyLimitMiddleware
from app.web.schemas import (
    AccessExchange,
    AccessLinkResponse,
    ErrorResponse,
    IntegrationJobResponse,
    JobResponse,
)

COOKIE = "contract_diff_session"
VIEW_COOKIE = "contract_diff_view"
INTEGRATION = "/api/v1/integrations/telegram/comparisons"


def get_service(request: Request) -> ComparisonService:
    """Получает уже собранный use-case из состояния приложения."""
    return request.app.state.service


Service = Annotated[ComparisonService, Depends(get_service)]


def capability(request: Request) -> str:
    """Читает HttpOnly-cookie; секрет не принимается в URL или JSON-задании."""
    value = request.cookies.get(COOKIE, "")
    if not 40 <= len(value) <= 100:
        raise HTTPException(403, "Откройте страницу сервиса для создания сессии")
    return value


def public_job(job) -> JobResponse:
    """Убирает хеш владельца и внутреннюю аренду из технической записи."""
    return JobResponse(**{k: v for k, v in asdict(job).items() if k in JobResponse.model_fields})


def read_result(job_id, request, service) -> dict:
    """Передаёт отдельную сессию чтения существующему сценарию выдачи результата."""
    return service.result(
        str(job_id), request.cookies.get(COOKIE, ""), viewer=request.cookies.get(VIEW_COOKIE, "")
    )


def create_app(
    settings: Settings | None = None, service: ComparisonService | None = None
) -> FastAPI:
    """Создаёт приложение с подменяемой сборкой для функциональных тестов."""
    settings = settings or Settings()
    service = service or build_service(settings)
    templates = Jinja2Templates(directory=str(ROOT / "app/web/templates"))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """Запускает уборку независимо от браузера; останавливает поток штатно."""
        stop = Event()

        def clean() -> None:
            """Периодически применяет TTL даже при недоступном RabbitMQ."""
            while not stop.is_set():
                with suppress(OSError):
                    service.cleanup()
                stop.wait(settings.cleanup_interval_seconds)

        thread = Thread(target=clean, name="ttl-cleaner", daemon=True)
        thread.start()
        yield
        stop.set()
        thread.join(timeout=5)

    app = FastAPI(
        title="Сверка редакций документов",
        version="0.1.0",
        lifespan=lifespan,
        responses={
            code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 410, 413, 422, 429, 503)
        },
    )
    app.state.service, app.state.settings = service, settings
    app.add_middleware(BodyLimitMiddleware, maximum=(settings.max_file_mb * 2 + 2) * 1024**2)
    app.mount("/static", StaticFiles(directory=ROOT / "app/web/static"), name="static")
    bearer = HTTPBearer(auto_error=False, scheme_name="TelegramIntegration")

    def integration_client(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        """Описывает серверную авторизацию в OpenAPI; фактическая проверка идёт до чтения multipart."""
        if credentials is None:
            raise HTTPException(
                401, "Требуется интеграционный токен", headers={"WWW-Authenticate": "Bearer"}
            )
        return settings.telegram_client_id

    @app.middleware("http")
    async def security(request: Request, call_next):
        """Защищает изменяющие запросы от CSRF и отключает кеширование документов."""
        integration = request.url.path == INTEGRATION or request.url.path.startswith(
            INTEGRATION + "/"
        )
        if integration:
            token = settings.telegram_integration_token.get_secret_value()
            if not token:
                return JSONResponse(
                    {"detail": "Интеграция не настроена", "code": "integration_unconfigured"},
                    status_code=503,
                )
            scheme, _, value = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not secrets.compare_digest(
                value.encode(), token.encode()
            ):
                return JSONResponse(
                    {"detail": "Неверный интеграционный токен", "code": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if request.headers.get("origin"):
                return JSONResponse(
                    {
                        "detail": "Интеграция доступна только серверному клиенту",
                        "code": "server_only",
                    },
                    status_code=403,
                )
            maximum = (
                settings.telegram_uploads_per_minute
                if request.method == "POST" and request.url.path == INTEGRATION
                else settings.telegram_requests_per_minute
            )
            scope = f"telegram:{settings.telegram_client_id}:" + (
                "upload" if request.method == "POST" and request.url.path == INTEGRATION else "poll"
            )
            if not await run_in_threadpool(
                service.repository.rate_limit, scope, maximum, time.time()
            ):
                return JSONResponse(
                    {"detail": "Превышена частота запросов", "code": "rate_limit"},
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
        if not integration and request.method in {"POST", "DELETE", "PUT", "PATCH"}:
            if request.headers.get("x-requested-with") != "ContractDiff":
                return JSONResponse({"detail": "Не подтверждён источник запроса"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Межсайтовый запрос запрещён"}, status_code=403)
            if request.url.path == "/api/v1/result-access":
                address = request.client.host if request.client else "unknown"
                if not await run_in_threadpool(
                    service.repository.rate_limit,
                    "exchange:" + address,
                    settings.access_exchange_per_minute,
                    time.time(),
                ):
                    return JSONResponse(
                        {"detail": "Превышена частота запросов", "code": "rate_limit"},
                        status_code=429,
                        headers={"Retry-After": "60"},
                    )
        response = await call_next(request)
        if request.url.path in {"/", "/api/v1/session"} and not request.cookies.get(COOKIE):
            response.set_cookie(
                COOKIE,
                secrets.token_urlsafe(48),
                httponly=True,
                secure=settings.cookie_secure,
                samesite="strict",
                max_age=settings.job_timeout_seconds + settings.retention_seconds + 3600,
                path="/",
            )
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    @app.exception_handler(DocumentError)
    async def document_error(request: Request, error: DocumentError):
        """Преобразует ожидаемые ошибки документа без утечки технического traceback."""
        status = 410 if "Срок хранения" in str(error) else error.status_code
        return JSONResponse({"detail": str(error), "code": error.code}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        """Не отражает значения билетов и multipart-полей в ошибках проверки."""
        return JSONResponse(
            {"detail": "Некорректные параметры запроса", "code": "invalid_request"}, status_code=422
        )

    @app.exception_handler(AccessDenied)
    async def access_error(request: Request, error: AccessDenied):
        """Не раскрывает существование чужого задания."""
        return JSONResponse({"detail": "Задание не найдено"}, status_code=404)

    @app.get("/", include_in_schema=False)
    @app.get("/open", include_in_schema=False)
    def index(request: Request):
        """Показывает согласованную страницу без фиктивных результатов и профиля."""
        return templates.TemplateResponse(
            request=request,
            name="pages/comparison.html",
            context={"max_file_mb": settings.max_file_mb, "max_pages": settings.max_pages},
        )

    @app.get("/api/v1/session")
    def session() -> dict:
        """Создаёт capability-cookie для программного клиента без авторизации пользователя."""
        return {"max_file_mb": settings.max_file_mb, "max_pages": settings.max_pages}

    @app.get("/healthz")
    def health() -> dict:
        """Проверяет живость HTTP-процесса без вызова внешней VLM."""
        return {"status": "ok"}

    @app.get("/readyz")
    def readiness():
        """Проверяет инструменты и соединение с обязательной очередью; VLM необязательна."""
        ready = bool(
            shutil.which(settings.tesseract_command) and shutil.which(settings.soffice_command)
        )
        try:
            connection = service.publisher.client.connection(connect_timeout=2)
            connection.ensure_connection(max_retries=0)
            connection.close()
        except Exception:
            ready = False
        return JSONResponse(
            {"status": "ready" if ready else "not_ready"}, status_code=200 if ready else 503
        )

    @app.post(
        INTEGRATION,
        status_code=202,
        response_model=IntegrationJobResponse,
        tags=["Telegram integration"],
        responses={
            401: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    def integration_submit(
        client: Annotated[str, Depends(integration_client)],
        old: Annotated[UploadFile, File()],
        new: Annotated[UploadFile, File()],
        idempotency_key: Annotated[
            str, Header(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
        ],
        ai_full: Annotated[bool, Form()] = False,
        ai_unverified: Annotated[bool, Form()] = False,
    ):
        """Передаёт нашу и контрагентскую версии существующему сценарию сравнения без перестановки."""
        try:
            job = service.submit(
                "",
                old.filename or "",
                old.file,
                new.filename or "",
                new.file,
                ai_full=ai_full,
                ai_unverified=ai_unverified,
                integration_client=client,
                idempotency_key=idempotency_key,
                max_active_jobs=settings.telegram_max_active_jobs,
            )
            return service.integration_status(job.id, client)
        finally:
            old.file.close()
            new.file.close()

    @app.get(
        INTEGRATION + "/{job_id}",
        response_model=IntegrationJobResponse,
        tags=["Telegram integration"],
    )
    def integration_status(job_id: UUID, client: Annotated[str, Depends(integration_client)]):
        """Позволяет боту опрашивать только собственное задание, не получая документы."""
        return service.integration_status(str(job_id), client)

    @app.post(
        INTEGRATION + "/{job_id}/access-link",
        response_model=AccessLinkResponse,
        tags=["Telegram integration"],
    )
    def integration_link(job_id: UUID, client: Annotated[str, Depends(integration_client)]):
        """Создаёт одноразовый fragment-билет для готового результата клиента."""
        values = service.access_link(
            str(job_id), client, settings.public_base_url, settings.access_link_seconds
        )
        for key in ("link_expires_at", "result_expires_at"):
            values[key] = datetime.fromtimestamp(values[key], UTC)
        return values

    @app.post("/api/v1/result-access", include_in_schema=False)
    def exchange_access(body: AccessExchange):
        """Обменивает одноразовый билет на HttpOnly-cookie чтения одного результата."""
        job_id, session, expires = service.activate_link(body.ticket)
        response = JSONResponse({"job_id": job_id, "read_only": True})
        response.set_cookie(
            VIEW_COOKIE,
            session,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=max(1, int(expires - time.time())),
            path="/",
        )
        return response

    @app.post("/api/v1/comparisons", status_code=202, response_model=JobResponse)
    def submit(
        request: Request,
        service: Service,
        old: Annotated[UploadFile, File()],
        new: Annotated[UploadFile, File()],
        use_ai: Annotated[bool, Form()] = False,
        ai_full: Annotated[bool | None, Form()] = None,
        ai_unverified: Annotated[bool, Form()] = False,
    ):
        """Сохраняет два проверенных файла и немедленно возвращает фоновое задание."""
        try:
            return public_job(
                service.submit(
                    capability(request),
                    old.filename or "",
                    old.file,
                    new.filename or "",
                    new.file,
                    use_ai,
                    ai_full=ai_full,
                    ai_unverified=ai_unverified,
                )
            )
        finally:
            old.file.close()
            new.file.close()

    @app.get("/api/v1/comparisons/{job_id}", response_model=JobResponse)
    def status(job_id: UUID, request: Request, service: Service):
        """Возвращает реальную стадию; клиент использует ограниченный polling."""
        return public_job(
            service.owned_job(
                str(job_id),
                request.cookies.get(COOKIE, ""),
                viewer=request.cookies.get(VIEW_COOKIE, ""),
            )
        )

    @app.get("/api/v1/comparisons/{job_id}/result")
    def result(job_id: UUID, request: Request, service: Service):
        """Выдаёт сводку без тяжёлой модели слов, серверных путей и секретов."""
        return queries.summary(read_result(job_id, request, service))

    @app.get("/api/v1/comparisons/{job_id}/differences")
    def differences(
        job_id: UUID,
        request: Request,
        service: Service,
        category: str = "",
        status: str = "",
        offset: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=500),
        show_unverified: bool = True,
    ):
        """Фильтрует уникальные изменения; поддерживает постраничную загрузку длинных результатов."""
        return queries.filter_differences(
            read_result(job_id, request, service),
            category,
            status,
            offset,
            limit,
            show_unverified,
        )

    @app.get("/api/v1/comparisons/{job_id}/differences/{difference_id}")
    def difference(job_id: UUID, difference_id: str, request: Request, service: Service):
        """Получает одно изменение с текстом и областями обеих сторон."""
        items = read_result(job_id, request, service)["differences"]
        found = next((d for d in items if d["id"] == difference_id), None)
        if found is None:
            raise HTTPException(404, "Изменение не найдено")
        return found

    @app.get("/api/v1/comparisons/{job_id}/preview/{side}")
    def preview(job_id: UUID, side: Literal["old", "new"], request: Request, service: Service):
        """Отдаёт PDF через FileResponse с Range, без загрузки целого файла в память API."""
        read_result(job_id, request, service)
        return FileResponse(
            service.storage.directory(str(job_id)) / side / "preview.pdf",
            media_type="application/pdf",
        )

    @app.get("/api/v1/comparisons/{job_id}/pages/{side}/{page}")
    def page_image(
        job_id: UUID,
        side: Literal["old", "new"],
        page: int,
        request: Request,
        service: Service,
        scale: float = Query(1.5, ge=0.5, le=3),
    ):
        """Лениво рендерит одну страницу для эквивалентного PDF-просмотрщика."""
        data = read_result(job_id, request, service)
        if page < 1 or page > len(data[side]["pages"]):
            raise HTTPException(404, "Страница не найдена")
        image = render_page(Path(data[side]["pdf"]), page, scale, settings.max_page_pixels)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        image.close()
        return Response(buffer.getvalue(), media_type="image/png")

    @app.get("/api/v1/comparisons/{job_id}/search/{side}")
    def search(
        job_id: UUID,
        side: Literal["old", "new"],
        request: Request,
        service: Service,
        q: str = Query(min_length=1, max_length=120),
    ):
        """Ищет по извлечённому тексту/OCR с областями, а не по скрытому слою подписанного PDF."""
        return queries.search_text(read_result(job_id, request, service), side, q)

    @app.get("/api/v1/comparisons/{job_id}/regions/{side}/{page}")
    def page_regions(
        job_id: UUID,
        side: Literal["old", "new"],
        page: int,
        request: Request,
        service: Service,
        show_unverified: bool = True,
    ):
        """Возвращает все маркеры текущей страницы независимо от фильтра и пагинации списка."""
        return queries.page_regions(
            read_result(job_id, request, service), side, page, show_unverified
        )

    @app.get("/api/v1/comparisons/{job_id}/correspondence/{side}/{page}")
    def correspondence(
        job_id: UUID, side: Literal["old", "new"], page: int, request: Request, service: Service
    ):
        """Определяет связанную страницу по совпавшим элементам, без равенства номеров страниц."""
        return queries.correspondence(read_result(job_id, request, service), side, page)

    @app.get("/api/v1/comparisons/{job_id}/export/{side}")
    def export(
        job_id: UUID,
        side: Literal["old", "new"],
        request: Request,
        service: Service,
        format: Literal["pdf", "docx"] = "pdf",
        show_unverified: bool = True,
    ):
        """Выдаёт только реально созданный и проверенный артефакт владельцу задания."""
        data = read_result(job_id, request, service)
        if not data.get("exports_current", True):
            raise HTTPException(409, "Экспорт с текущими результатами ИИ ещё готовится")
        item = next(
            (
                e
                for e in data["exports"].get(side, [])
                if e["format"] == format
                and (
                    bool(e.get("confirmed_only"))
                    if format == "docx"
                    else e.get("show_unverified", True) == show_unverified
                )
            ),
            None,
        )
        if item is None or not item["path"]:
            raise HTTPException(409, item["reason"] if item else "Этот формат экспорта недоступен")
        return FileResponse(
            item["path"],
            filename=f"{side}-highlighted.docx"
            if format == "docx"
            else f"{side}-{'with-unverified' if show_unverified else 'changes-only'}.pdf",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if format == "docx"
            else "application/octet-stream",
            headers={
                "X-Marked-Changes": str(item.get("marked_changes", 0)),
                "X-Unmarked-Changes": str(item.get("unmarked_changes", 0)),
            }
            if format == "docx"
            else None,
        )

    @app.get("/api/v1/comparisons/{job_id}/crop/{side}/{difference_id}")
    def graphic_crop(
        job_id: UUID,
        side: Literal["old", "new"],
        difference_id: str,
        request: Request,
        service: Service,
    ):
        """Показывает локальную графическую область без раскрытия файлового пути."""
        data = read_result(job_id, request, service)
        difference = next((d for d in data["differences"] if d["id"] == difference_id), None)
        if difference is None or not difference[f"{side}_regions"]:
            raise HTTPException(404, "Фрагмент отсутствует")
        region = difference[f"{side}_regions"][0]
        page = data[side]["pages"][region["page"] - 1]
        image = render_page(Path(data[side]["pdf"]), region["page"], 2, settings.max_page_pixels)
        crop = image.crop(
            (
                int(region["x0"] * image.width / page["width"]),
                int(region["y0"] * image.height / page["height"]),
                int(region["x1"] * image.width / page["width"]),
                int(region["y1"] * image.height / page["height"]),
            )
        )
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        crop.close()
        image.close()
        return Response(buffer.getvalue(), media_type="image/png")

    @app.post("/api/v1/comparisons/{job_id}/cancel", response_model=JobResponse)
    def cancel(job_id: UUID, request: Request, service: Service):
        """Отменяет только принадлежащее сессии задание."""
        return public_job(service.cancel(str(job_id), capability(request)))

    @app.post("/api/v1/comparisons/{job_id}/ai/retry", response_model=JobResponse, status_code=202)
    def retry_ai(job_id: UUID, request: Request, service: Service):
        """Повторяет незавершённую ИИ-проверку владельца без повторной загрузки и OCR."""
        return public_job(service.retry_ai(str(job_id), capability(request)))

    @app.get("/api/v1/comparisons/{job_id}/ai/operations")
    def ai_operations(
        job_id: UUID,
        request: Request,
        service: Service,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ):
        """Показывает владельцу фактические технические результаты отдельных операций ИИ."""
        return queries.ai_operations(read_result(job_id, request, service), offset, limit)

    @app.delete("/api/v1/comparisons/{job_id}", status_code=204)
    def delete(job_id: UUID, request: Request, service: Service):
        """Немедленно прекращает доступ к результату и инициирует очистку."""
        service.delete(str(job_id), capability(request))
        return Response(status_code=204)

    return app
