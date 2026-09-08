FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev

FROM python:3.13-slim

ARG VERSION="v4.7.0" # x-release-please-version
ARG BUILD_DATE
ARG VCS_REF

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# База — плавающий тег python:3.13-slim, и между её пересборками Debian успевает
# выпустить исправления системных пакетов (util-linux, zlib, PCRE2 в отчётах
# Trivy). Ставим их на этапе сборки: иначе образ уезжает с дырами, которые в
# апстриме уже закрыты, а сама база подтянется неизвестно когда.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# Системные pip и setuptools базового образа приложению не нужны (зависимости в .venv), а Trivy
# находит в них CVE (setuptools 70.3.0, msgpack внутри pip) — убираем из образа.
RUN /usr/local/bin/python -m pip uninstall -y setuptools pip

RUN groupadd -g 1000 app && \
    useradd -u 1000 -g 1000 -m -s /bin/bash app

WORKDIR /app

COPY --chown=app:app . .

RUN mkdir -p logs data uploads/images uploads/videos uploads/thumbnails locales && \
    chown -R app:app logs data uploads locales

USER app

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VERSION=${VERSION} \
    BUILD_DATE=${BUILD_DATE} \
    VCS_REF=${VCS_REF}

EXPOSE 8080

LABEL org.opencontainers.image.title="Bedolaga RemnaWave Bot" \
      org.opencontainers.image.description="Telegram bot for RemnaWave VPN service" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/fr1ngg/remnawave-bedolaga-telegram-bot" \
      org.opencontainers.image.url="https://github.com/fr1ngg/remnawave-bedolaga-telegram-bot" \
      org.opencontainers.image.vendor="fr1ngg"

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["python", "main.py"]
