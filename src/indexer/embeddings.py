"""Precomputed MiniLM embeddings, one set per name, in data/embeddings/:

    data/embeddings/<name>.npy   one unit-length row per text, float16
    data/embeddings/<name>.json  how it was built: rows, dim, the model, and the SHA-256 of the
                                 model, the tokenizer and every source file

    write_set("topics", texts, [Path("data/topics/topics.txt")], embed, model, tokenizer)
    matrix = read_set("topics", len(texts), [Path("data/topics/topics.txt")], model, tokenizer)

`read_set` refuses a set built from other sources, another model or tokenizer, or with another
number of rows, so a stale matrix is never used. Sources are recorded by the path as given (keep
them relative to the repository root). Sets are rebuilt with `indexer embeddings` (the
embeddings workflow); never edit them by hand.

MiniLM is all-MiniLM-L6-v2, quantized ONNX (Apache-2.0); the Docker image ships it and sets
$INDEXER_MINILM_MODEL and $INDEXER_MINILM_TOKENIZER (`model_paths`).
"""

import hashlib
import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import numpy.typing as npt

EMBEDDINGS_DIR = Path("data/embeddings")
MODEL_ENV = "INDEXER_MINILM_MODEL"  # the Docker image sets both
TOKENIZER_ENV = "INDEXER_MINILM_TOKENIZER"
DEFAULT_MODEL = Path("/opt/minilm.onnx")
DEFAULT_TOKENIZER = Path("/opt/minilm-tokenizer.json")
MODEL_NAME = "Xenova/all-MiniLM-L6-v2 (quantized)"
MAX_TOKENS = 128  # per text (an item's text is up to 300 characters, about 70 tokens)
BATCH = 256  # texts embedded at a time when building a set

Vectors = npt.NDArray[np.float32]
# Texts -> one unit-length row per text.
Embed = Callable[[Sequence[str]], Vectors]


class EmbeddingsError(Exception):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def model_paths(model: Path | None, tokenizer: Path | None) -> tuple[Path, Path]:
    """MiniLM's files: as given, else $INDEXER_MINILM_MODEL and $INDEXER_MINILM_TOKENIZER, else
    the image's defaults."""
    return (
        model or Path(os.environ.get(MODEL_ENV) or DEFAULT_MODEL),
        tokenizer or Path(os.environ.get(TOKENIZER_ENV) or DEFAULT_TOKENIZER),
    )


def load_embedder(model: Path, tokenizer: Path) -> Embed:
    """MiniLM: mean of the token vectors (padding excluded), scaled to unit length."""
    import onnxruntime as ort  # type: ignore[import-untyped]  # ships no type hints
    from tokenizers import Tokenizer

    ort.set_default_logger_severity(3)  # errors only (it warns it can't save telemetry state)
    ort.disable_telemetry_events()
    tok = Tokenizer.from_file(str(tokenizer))
    tok.enable_truncation(MAX_TOKENS)
    tok.enable_padding()
    session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    inputs = {i.name for i in session.get_inputs()}

    def embed(texts: Sequence[str]) -> Vectors:
        encoded = tok.encode_batch(list(texts))
        ids = np.array([e.ids for e in encoded], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = np.asarray(session.run(None, feed)[0], dtype=np.float32)
        pooled = (hidden * mask[..., None]).sum(1) / np.maximum(mask.sum(1, keepdims=True), 1)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.maximum(norms, 1e-12)).astype(np.float32)

    return embed


def set_paths(name: str, directory: Path = EMBEDDINGS_DIR) -> tuple[Path, Path]:
    """(matrix, metadata) of the set `name`: <directory>/<name>.npy and <name>.json."""
    return directory / f"{name}.npy", directory / f"{name}.json"


def write_set(
    name: str,
    texts: Sequence[str],
    sources: Sequence[Path],
    embed: Embed,
    model: Path,
    tokenizer: Path,
    directory: Path = EMBEDDINGS_DIR,
) -> int:
    """Embed `texts` (in order) and write the set `name`: the matrix (float16, unit length, one
    row per text) and its metadata (the hashes `read_set` checks). `sources` are the files the
    texts come from. Returns the number of rows."""
    if not texts:
        raise EmbeddingsError(f"{name}: no texts to embed")
    matrix = np.vstack([embed(texts[i : i + BATCH]) for i in range(0, len(texts), BATCH)])
    matrix_path, meta_path = set_paths(name, directory)
    directory.mkdir(parents=True, exist_ok=True)
    np.save(matrix_path, matrix.astype(np.float16))
    meta = {
        "rows": len(texts),
        "dim": int(matrix.shape[1]),
        "dtype": "float16",
        "model": MODEL_NAME,
        "model_sha256": sha256(model),
        "tokenizer_sha256": sha256(tokenizer),
        "sources": {source.as_posix(): sha256(source) for source in sources},
    }
    meta_path.write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return len(texts)


def read_set(
    name: str,
    rows: int,
    sources: Sequence[Path],
    model: Path,
    tokenizer: Path,
    directory: Path = EMBEDDINGS_DIR,
) -> Vectors:
    """The matrix of the set `name` (float32); EmbeddingsError if it was built from other
    sources, another model or tokenizer, or doesn't have `rows` rows."""
    matrix_path, meta_path = set_paths(name, directory)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    expected = {
        "model_sha256": sha256(model),
        "tokenizer_sha256": sha256(tokenizer),
        "sources": {source.as_posix(): sha256(source) for source in sources},
    }
    for key, value in expected.items():
        if meta.get(key) != value:
            raise EmbeddingsError(f"{matrix_path} is stale: its {key} changed since it was built")
    matrix: Vectors = np.asarray(np.load(matrix_path), dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] != rows:
        raise EmbeddingsError(f"{matrix_path} has shape {matrix.shape}, expected {rows} rows")
    return matrix
