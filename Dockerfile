# indexer image for the `check` compose service. Exact versions pinned; both publish arm64.
FROM python:3.13.18-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first (cached until uv.lock changes), then the project itself.
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13.18-slim-trixie
RUN useradd --system --uid 10001 --no-create-home indexer
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER indexer
WORKDIR /work
ENTRYPOINT ["indexer"]
CMD ["run", "data/urls.txt"]
