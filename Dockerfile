# Multi-stage build: dependencies are resolved once in the builder and the
# runtime image ships only the virtualenv plus the package. No compiler, no
# build cache, no root.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /build
RUN python -m venv "$VIRTUAL_ENV"

# Dependency metadata first, so a source-only change reuses the wheel layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .


FROM python:3.12-slim AS runtime

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SENTINEL_DATA_DIR=/data \
    SENTINEL_LOG_FORMAT=json

RUN useradd --create-home --uid 10001 sentinel \
    && mkdir -p /data \
    && chown -R sentinel:sentinel /data

COPY --from=builder --chown=sentinel:sentinel /opt/venv /opt/venv

USER sentinel
WORKDIR /home/sentinel
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["sentinel"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
