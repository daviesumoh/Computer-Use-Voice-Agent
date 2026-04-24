# Build from the services/ directory, so the shared package is in the build context:
#   docker build -f worker/Dockerfile -t worker .
# Version must match the playwright pin in uv.lock: the image ships the matching browsers.
FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/app/worker/.venv/bin:$PATH

# pyproject resolves the shared package as ../common.
COPY common /app/common
WORKDIR /app/worker

# Dependencies first, so code changes don't reinstall them.
COPY worker/pyproject.toml worker/uv.lock worker/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY worker/src ./src
RUN uv sync --frozen --no-dev
COPY worker/with-display.sh /usr/local/bin/with-display

USER pwuser
EXPOSE 8000

# Headed Chromium on a virtual display; see browser_smoke.py for why not headless.
# The host-sizing check is `docker run ... worker browser-smoke --memory-limit-mb 1024`.
ENTRYPOINT ["with-display"]
CMD ["uvicorn", "--factory", "worker.api:build", "--host", "0.0.0.0", "--port", "8000"]
