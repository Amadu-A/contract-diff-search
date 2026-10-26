<!-- docs/dependencies.md -->
# Зависимости и лицензии

Версии фиксируются `uv.lock`, Docker устанавливает экспортированные
`requirements.lock` / `requirements-dev.lock` с хешами. Их следует обновлять
согласованно: `uv lock`, затем `uv export --frozen --no-dev --no-emit-project -o requirements.lock`
и `uv export --frozen --only-dev --no-emit-project -o requirements-dev.lock`.

| Компонент | Назначение | Условия верхнего уровня |
|---|---|---|
| FastAPI, Pydantic, pydantic-settings | API и конфигурация | MIT |
| Jinja2 | Автоэкранированные шаблоны | BSD-3-Clause |
| pypdf | PDF-структура, подписи, отдельный экспорт | BSD-3-Clause |
| pypdfium2 / PDFium | Рендер с видимой геометрией | BSD-3-Clause / Apache-2.0 и third-party notices |
| pdfplumber / pdfminer.six | Текст, символы, координаты | MIT |
| lxml / libxml2 | Безопасный OOXML | BSD-3-Clause / MIT |
| Pillow | Растровые области | Лицензия Pillow (MIT-CMU/HPND по версии), third-party notices |
| NumPy, SciPy | Морфология, геометрия, DCT/SSIM | BSD и лицензии включённых компонентов |
| ReportLab | Координатные PDF-слои | BSD |
| olefile | Проверка бинарного DOC и VBA-структур | BSD |
| Celery, Kombu | Доставка общим RabbitMQ | BSD |
| HTTPX | Корпоративный VLM-клиент | BSD-3-Clause |
| Tesseract | Независимый русский/английский OCR | Apache-2.0 |
| LibreOffice | Отдельный headless-процесс Word-конвертации | MPL-2.0 / LGPLv3+ и notices дистрибутива |
| Playwright | Изолированные браузерные тесты | Apache-2.0 |

PyMuPDF не используется: выбор PDFium/pdfplumber/pypdf избегает добавления
PyMuPDF с AGPL-условиями. Документ не заменяет проверку условий конкретной
поставки: дистрибутивы PDFium, LibreOffice, Chromium и системных шрифтов включают
собственные notices, которые должны сохраняться при распространении образа.

Первичные источники: [pypdf](https://github.com/py-pdf/pypdf),
[pypdfium2](https://github.com/pypdfium2-team/pypdfium2),
[pdfplumber](https://github.com/jsvine/pdfplumber),
[Tesseract](https://github.com/tesseract-ocr/tesseract),
[LibreOffice](https://www.libreoffice.org/about-us/licenses/).
