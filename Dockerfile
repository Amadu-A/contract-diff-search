# Dockerfile
# Один образ API, worker и уборки; OCR и LibreOffice без запуска от root.
FROM python:3.12-slim-bookworm AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HOME=/tmp/contract-diff OMP_THREAD_LIMIT=1 OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 BLIS_NUM_THREADS=1 \
    VECLIB_MAXIMUM_THREADS=1
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
    libreoffice-writer tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    fonts-dejavu-core fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 app && useradd -u 10001 -g app -M app
WORKDIR /srv/app
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock
ENV PYTHONPYCACHEPREFIX=/tmp/python-cache
RUN mkdir -p /srv/app/.data /srv/app/.cache && chown app:app /srv/app/.data /srv/app/.cache

FROM dependencies AS runtime
COPY --chown=app:app app ./app
COPY --chown=app:app .env.example pyproject.toml ./
USER app
EXPOSE 8082
CMD ["python", "-m", "app"]

FROM dependencies AS test
USER root
RUN apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 install -y --no-install-recommends chromium nodejs bash poppler-utils \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.lock ./
RUN pip install --require-hashes -r requirements-dev.lock
COPY --chown=app:app app ./app
COPY --chown=app:app .env.example pyproject.toml ./
COPY --chown=app:app tests ./tests
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app package.json ./
USER app
ENV COVERAGE_FILE=/tmp/.coverage
CMD ["sh", "-c", "python -m pytest -q -m 'not real and not external' --cov=app --cov-branch --cov-report=term --basetemp=/tmp/pytest && node --test tests/frontend/*.test.js"]
