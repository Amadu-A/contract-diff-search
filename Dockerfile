# Dockerfile
# Один образ API, worker и уборки; OCR и LibreOffice без запуска от root.
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HOME=/tmp/contract-diff OMP_THREAD_LIMIT=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    fonts-dejavu-core fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 app && useradd -u 10001 -g app -M app
WORKDIR /srv/app
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock
COPY --chown=app:app app ./app
COPY --chown=app:app .env.example pyproject.toml ./
RUN mkdir -p /srv/app/.data && chown app:app /srv/app/.data
USER app
EXPOSE 8082
CMD ["python", "-m", "app"]

FROM runtime AS test
USER root
RUN apt-get update && apt-get install -y --no-install-recommends chromium nodejs bash \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.lock ./
RUN pip install --require-hashes -r requirements-dev.lock
COPY --chown=app:app tests ./tests
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app package.json ./
USER app
CMD ["sh", "-c", "python -m pytest -q -m 'not real and not external' --basetemp=/tmp/pytest && node --test tests/frontend/*.test.js"]
