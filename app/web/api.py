# app/web/api.py
"""Тонкий HTTP transport: сессионный доступ, загрузка и выдача временных результатов."""

import io
import secrets
import shutil
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from threading import Event, Thread
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.application import queries
from app.application.service import ComparisonService
from app.bootstrap import build_service
from app.domain.models import AccessDenied, DocumentError
from app.infrastructure.pdf import render_page
from app.settings import ROOT, Settings
from app.web.body_limit import BodyLimitMiddleware
from app.web.schemas import ErrorResponse, JobResponse

COOKIE = "contract_diff_session"


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
            403: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
        },
    )
    app.state.service, app.state.settings = service, settings
    app.add_middleware(BodyLimitMiddleware, maximum=(settings.max_file_mb * 2 + 2) * 1024**2)
    app.mount("/static", StaticFiles(directory=ROOT / "app/web/static"), name="static")

    @app.middleware("http")
    async def security(request: Request, call_next):
        """Защищает изменяющие запросы от CSRF и отключает кеширование документов."""
        if request.method in {"POST", "DELETE", "PUT", "PATCH"}:
            if request.headers.get("x-requested-with") != "ContractDiff":
                return JSONResponse({"detail": "Не подтверждён источник запроса"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Межсайтовый запрос запрещён"}, status_code=403)
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
        status = 410 if "Срок хранения" in str(error) else 422
        return JSONResponse({"detail": str(error)}, status_code=status)

    @app.exception_handler(AccessDenied)
    async def access_error(request: Request, error: AccessDenied):
        """Не раскрывает существование чужого задания."""
        return JSONResponse({"detail": "Задание не найдено"}, status_code=404)

    @app.get("/", include_in_schema=False)
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

    @app.post("/api/v1/comparisons", status_code=202, response_model=JobResponse)
    def submit(
        request: Request,
        service: Service,
        old: Annotated[UploadFile, File()],
        new: Annotated[UploadFile, File()],
        use_ai: Annotated[bool, Form()] = False,
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
                )
            )
        finally:
            old.file.close()
            new.file.close()

    @app.get("/api/v1/comparisons/{job_id}", response_model=JobResponse)
    def status(job_id: UUID, request: Request, service: Service):
        """Возвращает реальную стадию; клиент использует ограниченный polling."""
        return public_job(service.owned_job(str(job_id), capability(request)))

    @app.get("/api/v1/comparisons/{job_id}/result")
    def result(job_id: UUID, request: Request, service: Service):
        """Выдаёт сводку без тяжёлой модели слов, серверных путей и секретов."""
        return queries.summary(service.result(str(job_id), capability(request)))

    @app.get("/api/v1/comparisons/{job_id}/differences")
    def differences(
        job_id: UUID,
        request: Request,
        service: Service,
        category: str = "",
        status: str = "",
        offset: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=500),
    ):
        """Фильтрует уникальные изменения; поддерживает постраничную загрузку длинных результатов."""
        return queries.filter_differences(
            service.result(str(job_id), capability(request)), category, status, offset, limit
        )

    @app.get("/api/v1/comparisons/{job_id}/differences/{difference_id}")
    def difference(job_id: UUID, difference_id: str, request: Request, service: Service):
        """Получает одно изменение с текстом и областями обеих сторон."""
        items = service.result(str(job_id), capability(request))["differences"]
        found = next((d for d in items if d["id"] == difference_id), None)
        if found is None:
            raise HTTPException(404, "Изменение не найдено")
        return found

    @app.get("/api/v1/comparisons/{job_id}/preview/{side}")
    def preview(job_id: UUID, side: Literal["old", "new"], request: Request, service: Service):
        """Отдаёт PDF через FileResponse с Range, без загрузки целого файла в память API."""
        service.result(str(job_id), capability(request))
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
        data = service.result(str(job_id), capability(request))
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
        return queries.search_text(service.result(str(job_id), capability(request)), side, q)

    @app.get("/api/v1/comparisons/{job_id}/regions/{side}/{page}")
    def page_regions(
        job_id: UUID, side: Literal["old", "new"], page: int, request: Request, service: Service
    ):
        """Возвращает все маркеры текущей страницы независимо от фильтра и пагинации списка."""
        return queries.page_regions(service.result(str(job_id), capability(request)), side, page)

    @app.get("/api/v1/comparisons/{job_id}/correspondence/{side}/{page}")
    def correspondence(
        job_id: UUID, side: Literal["old", "new"], page: int, request: Request, service: Service
    ):
        """Определяет связанную страницу по совпавшим элементам, без равенства номеров страниц."""
        return queries.correspondence(service.result(str(job_id), capability(request)), side, page)

    @app.get("/api/v1/comparisons/{job_id}/export/{side}")
    def export(
        job_id: UUID,
        side: Literal["old", "new"],
        request: Request,
        service: Service,
        format: Literal["pdf", "doc", "docx"] = "pdf",
    ):
        """Выдаёт только реально созданный и проверенный артефакт владельцу задания."""
        data = service.result(str(job_id), capability(request))
        item = next((e for e in data["exports"].get(side, []) if e["format"] == format), None)
        if item is None or not item["path"]:
            raise HTTPException(409, item["reason"] if item else "Этот формат экспорта недоступен")
        return FileResponse(
            item["path"],
            filename=f"{side}-highlighted.{format}",
            media_type="application/octet-stream",
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
        data = service.result(str(job_id), capability(request))
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

    @app.delete("/api/v1/comparisons/{job_id}", status_code=204)
    def delete(job_id: UUID, request: Request, service: Service):
        """Немедленно прекращает доступ к результату и инициирует очистку."""
        service.delete(str(job_id), capability(request))
        return Response(status_code=204)

    return app
