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
# fastText's language identification model (lid.176, compressed; CC BY-SA 3.0, by Facebook AI
# Research), verified by its SHA-256. Bump the URL and checksum together.
ADD --checksum=sha256:8f3472cfe8738a7b6099e8e999c3cbfae0dcd15696aac7d7738a8039db603e83 --chmod=644 \
    https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz /opt/lid.176.ftz
# MiniLM (all-MiniLM-L6-v2, quantized ONNX, and its tokenizer; Apache-2.0) for topics, at a pinned
# revision and SHA-256. data/topics/topics.json records the same hashes: the topic matrix
# (data/topics/topics.npy) must be rebuilt whenever these change.
ADD --checksum=sha256:afdb6f1a0e45b715d0bb9b11772f032c399babd23bfc31fed1c170afc848bdb1 --chmod=644 \
    https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/751bff37182d3f1213fa05d7196b954e230abad9/onnx/model_quantized.onnx \
    /opt/minilm.onnx
ADD --checksum=sha256:da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0 --chmod=644 \
    https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/751bff37182d3f1213fa05d7196b954e230abad9/tokenizer.json \
    /opt/minilm-tokenizer.json
ENV PATH=/app/.venv/bin:$PATH \
    INDEXER_LID_MODEL=/opt/lid.176.ftz \
    INDEXER_MINILM_MODEL=/opt/minilm.onnx \
    INDEXER_MINILM_TOKENIZER=/opt/minilm-tokenizer.json \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER indexer
WORKDIR /work
ENTRYPOINT ["indexer"]
CMD ["run", "data/urls.txt"]
