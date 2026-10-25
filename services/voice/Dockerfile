# The build context holds common/ and voice/ side by side: the services/ directory, or the folder
# scripts/stage-voice.sh makes for LiveKit Cloud.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/app/voice/.venv/bin:$PATH

# pyproject resolves the shared package as ../common.
COPY common /app/common
WORKDIR /app/voice

COPY voice/pyproject.toml voice/uv.lock voice/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY voice/src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home agent
USER agent
# Model files the plugins load at start-up are fetched now, not on the first call.
RUN python -m voice.agent download-files

CMD ["python", "-m", "voice.agent", "start"]
