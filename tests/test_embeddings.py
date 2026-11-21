"""Embedding sets in data/embeddings/: written with their hashes, refused when stale. A fake
embedder stands in for MiniLM; nothing is downloaded."""

import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from indexer.cli import EMBEDDING_SETS, main
from indexer.embeddings import EmbeddingsError, Vectors, read_set, set_paths, write_set


def fake_embed(texts: Sequence[str]) -> Vectors:
    rows = np.array([[len(t), 1.0, 0.0] for t in texts], dtype=np.float32)
    return rows / np.linalg.norm(rows, axis=1, keepdims=True)


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path, Path]:
    model, tokenizer, source = tmp_path / "m.onnx", tmp_path / "t.json", tmp_path / "src.txt"
    model.write_bytes(b"model")
    tokenizer.write_bytes(b"tokenizer")
    source.write_text("a\nbb\n")
    return model, tokenizer, source


def test_write_then_read(tmp_path: Path, files: tuple[Path, Path, Path]) -> None:
    model, tokenizer, source = files
    out = tmp_path / "embeddings"
    assert write_set("things", ["a", "bb"], [source], fake_embed, model, tokenizer, out) == 2
    matrix_path, meta_path = set_paths("things", out)
    assert (matrix_path.name, meta_path.name) == ("things.npy", "things.json")
    assert np.load(matrix_path).dtype == np.float16
    meta = json.loads(meta_path.read_text())
    assert meta["rows"] == 2 and meta["dim"] == 3 and list(meta["sources"]) == [source.as_posix()]
    matrix = read_set("things", 2, [source], model, tokenizer, out)
    assert matrix.dtype == np.float32 and np.allclose(matrix, fake_embed(["a", "bb"]), atol=1e-3)


@pytest.mark.parametrize("changed", ["source", "model", "tokenizer"])
def test_a_stale_set_is_refused(
    tmp_path: Path, files: tuple[Path, Path, Path], changed: str
) -> None:
    model, tokenizer, source = files
    write_set("things", ["a"], [source], fake_embed, model, tokenizer, tmp_path)
    {"source": source, "model": model, "tokenizer": tokenizer}[changed].write_text("changed")
    with pytest.raises(EmbeddingsError, match="stale"):
        read_set("things", 1, [source], model, tokenizer, tmp_path)


def test_rows_and_empty_sets(tmp_path: Path, files: tuple[Path, Path, Path]) -> None:
    model, tokenizer, source = files
    write_set("things", ["a", "bb"], [source], fake_embed, model, tokenizer, tmp_path)
    with pytest.raises(EmbeddingsError, match="rows"):
        read_set("things", 3, [source], model, tokenizer, tmp_path)
    with pytest.raises(EmbeddingsError, match="no texts"):
        write_set("empty", [], [source], fake_embed, model, tokenizer, tmp_path)


def test_the_cli_knows_its_sets() -> None:
    assert list(EMBEDDING_SETS) == ["topics"]
    assert main(["embeddings", "nope"]) == 2  # unknown names are refused before building
