# app/web/body_limit.py
"""Ограничивает весь multipart-поток до его распаковки FastAPI/Starlette."""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    """Применяет общий лимит и к chunked-загрузкам без Content-Length."""

    def __init__(self, app, maximum: int):
        """Принимает ASGI-приложение и предельное число байтов с учётом multipart."""
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        """Останавливает превышение до записи неограниченного временного файла."""
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope.get("headers", [])).get(b"content-length", b"0")
        try:
            declared = int(length)
        except ValueError:
            return await JSONResponse({"detail": "Некорректный размер запроса"}, 400)(
                scope, receive, send
            )
        if declared < 0 or declared > self.maximum:
            return await JSONResponse({"detail": "Превышен размер загрузки"}, 413)(
                scope, receive, send
            )
        size = 0

        async def limited_receive():
            """Считает фактически полученные байты независимо от заявленного размера."""
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > self.maximum:
                raise HTTPException(413, "Превышен размер загрузки")
            return message

        return await self.app(scope, limited_receive, send)
