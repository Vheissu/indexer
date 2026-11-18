"""Topics from English item titles. A fake embedder and a tiny topic matrix stand in for MiniLM and
data/topics (tests never need the model or the network)."""

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from indexer.topics import (
    ABOUT_WEIGHT,
    AUTHOR_MIN_SCORE,
    MARGIN,
    MAX_ITEM_TAGS,
    MIN_SCORE,
    TOP,
    Post,
    TopicIndex,
    TopicModel,
    TopicsError,
    Vectors,
    about_text,
    author_topics,
    build_index,
    feed_topics,
    first_author_topic,
    load_index,
    name_text,
    read_synonyms,
    tag_key,
)

AXES = ("rust", "python", "food", "music", "cars", "film", "art")


def unit(*weights: float) -> Vectors:
    v = np.array(weights, dtype=np.float32)
    return v / np.linalg.norm(v)


def fake_embed(texts: Sequence[str]) -> Vectors:
    """Each text points along the axes of the words it contains (a word per axis)."""
    rows = []
    for text in texts:
        words = text.lower().split()
        rows.append(unit(*[1.0 if axis in words else 0.0 for axis in AXES] + [0.01]))
    return np.array(rows, dtype=np.float32)


def index(names: Sequence[str], synonyms: dict[str, str] | None = None) -> TopicIndex:
    """One topic per name, along the axis named like it (or like its first word)."""
    matrix = np.array(
        [unit(*[1.0 if n.split("-")[0] == a else 0.0 for a in AXES] + [0.01]) for n in names],
        dtype=np.float32,
    )
    return TopicIndex(tuple(names), matrix, synonyms or {})


def test_the_closest_topics_most_similar_first() -> None:
    idx = index(["food", "rust", "python", "music", "cars", "film", "art"])
    assert feed_topics(["rust ownership", "rust lifetimes"], fake_embed, idx) == ["rust"]
    # two topics equally present: both kept (each within MARGIN of the best)
    topics = feed_topics(["rust and python", "python and rust"], fake_embed, idx)
    assert set(topics) == {"rust", "python"}


def test_only_topics_close_to_the_best_are_kept() -> None:
    idx = index(["food", "rust", "python", "music", "cars", "film", "art"])
    titles = ["rust ownership", "rust lifetimes", "rust and python"]  # mostly rust
    assert feed_topics(titles, fake_embed, idx) == ["rust"]  # python is more than MARGIN behind


def test_a_vague_feed_gets_no_topics() -> None:
    # every topic about equally close: nothing stands out by MIN_SPREAD
    idx = index(["food", "rust", "python", "music", "cars", "film", "art", "rust-2"])
    assert feed_topics([" ".join(AXES)], fake_embed, idx) == []


def test_nothing_close_enough() -> None:
    idx = index(["food", "music"])
    assert feed_topics(["rust tips", "python tricks"], fake_embed, idx) == []


def test_at_most_top_topics() -> None:
    names = [f"rust-{i}" for i in range(TOP + 2)] + ["food", "music"]  # 7 equal, then far
    topics = feed_topics(["rust tips", "rust news"], fake_embed, index(names))
    assert len(topics) == TOP
    assert MARGIN > 0 and MIN_SCORE < 1


def test_synonyms_map_to_one_canonical_name() -> None:
    idx = index(["rust", "rust-lang", "food"], {"rust-lang": "rust"})
    assert feed_topics(["rust news", "rust release"], fake_embed, idx) == ["rust"]


def test_no_titles() -> None:
    idx = index(["rust"])
    assert feed_topics([], fake_embed, idx) == []
    assert feed_topics(["  ", ""], fake_embed, idx) == []


def test_read_synonyms_resolves_chains_and_skips_comments(tmp_path: Path) -> None:
    path = tmp_path / "synonyms.tsv"
    path.write_text("# a comment\nfloss\tfoss\nfoss\topen-source\nx\ty\ny\tx\n\nbad line\n")
    syn = read_synonyms(path)
    assert syn["floss"] == "open-source" and syn["foss"] == "open-source"
    assert syn["x"] in {"x", "y"}  # a loop ends instead of hanging
    assert "bad line" not in syn


def write_index(directory: Path, names: Sequence[str], model: Path, tokenizer: Path) -> None:
    directory.mkdir(exist_ok=True)
    (directory / "topics.txt").write_text("".join(f"{n}\n" for n in names))
    np.save(directory / "topics.npy", np.ones((len(names), 4), dtype=np.float16))
    (directory / "synonyms.tsv").write_text("old\tnew\n")

    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    meta = {
        "topics_sha256": sha(directory / "topics.txt"),
        "model_sha256": sha(model),
        "tokenizer_sha256": sha(tokenizer),
    }
    (directory / "topics.json").write_text(json.dumps(meta))


def test_load_index(tmp_path: Path) -> None:
    model, tokenizer = tmp_path / "m.onnx", tmp_path / "t.json"
    model.write_bytes(b"model")
    tokenizer.write_bytes(b"tokenizer")
    write_index(tmp_path / "tags", ["a", "b"], model, tokenizer)
    idx = load_index(tmp_path / "tags", model, tokenizer)
    assert idx.names == ("a", "b") and idx.matrix.shape == (2, 4)
    assert idx.matrix.dtype == np.float32 and idx.synonyms == {"old": "new"}


@pytest.mark.parametrize("changed", ["topics", "model", "tokenizer"])
def test_a_stale_matrix_is_refused(tmp_path: Path, changed: str) -> None:
    model, tokenizer = tmp_path / "m.onnx", tmp_path / "t.json"
    model.write_bytes(b"model")
    tokenizer.write_bytes(b"tokenizer")
    tags = tmp_path / "tags"
    write_index(tags, ["a", "b"], model, tokenizer)
    {
        "topics": tags / "topics.txt",
        "model": model,
        "tokenizer": tokenizer,
    }[changed].write_text("changed\n")
    with pytest.raises(TopicsError, match="stale"):
        load_index(tags, model, tokenizer)


def test_a_matrix_without_one_row_per_name_is_refused(tmp_path: Path) -> None:
    model, tokenizer = tmp_path / "m.onnx", tmp_path / "t.json"
    model.write_bytes(b"model")
    tokenizer.write_bytes(b"tokenizer")
    tags = tmp_path / "tags"
    write_index(tags, ["a", "b"], model, tokenizer)
    np.save(tags / "topics.npy", np.ones((3, 4), dtype=np.float16))
    with pytest.raises(TopicsError, match="rows"):
        load_index(tags, model, tokenizer)


def test_tag_key_and_topic_of() -> None:
    assert tag_key("#AI") == "ai" and tag_key("Web Dev") == "webdev" and tag_key("C++") == "c++"
    assert tag_key("Café") == "cafe" and tag_key("  ") == ""
    idx = index(["web-dev", "books", "rust", "rust-lang"], {"rust-lang": "rust"})
    assert idx.topic_of("Web_Dev") == "web-dev"
    assert idx.topic_of("book") == "books" and idx.topic_of("Book") == "books"  # singular, case
    assert idx.topic_of("Rust Lang") == "rust"  # a synonym's spelling
    assert idx.topic_of("journal") is None and idx.topic_of("") is None


def test_author_topics_frequency_and_stuffing() -> None:
    idx = index(["rust", "python", "food", "music"])
    tags = [["Rust"], ["rust", "food"], ["python"], [], []]
    assert author_topics(tags, idx) == {"rust"}  # 2 of 5; python and food only once
    assert author_topics([["rust"], ["rust"], *[[]] * 9], idx) == set()  # 2 of 11: under 20%
    stuffed = [["rust", *(f"x{i}" for i in range(MAX_ITEM_TAGS))]] * 5  # 11 tags each: ignored
    assert author_topics(stuffed, idx) == set()


def test_author_backed_topics_are_added_when_plausible() -> None:
    idx = index(["food", "rust", "python", "music", "cars", "film", "art"])
    texts = ["rust ownership", "rust and python", "rust and python"]  # rust 0.86, python 0.50
    assert feed_topics(texts, fake_embed, idx) == ["rust"]  # python is beyond MARGIN
    tags = [["Python"], ["python"], ["food"]]
    # python is plausible (scores well above AUTHOR_MIN_SCORE): added after the model's picks
    assert feed_topics(texts, fake_embed, idx, tags) == ["rust", "python"]
    # food is backed by 2 items but implausible for these posts (scores ~0): not added
    assert feed_topics(texts, fake_embed, idx, [["food"], ["food"], []]) == ["rust"]
    assert AUTHOR_MIN_SCORE > 0.0


def test_author_backed_topics_fill_in_when_the_model_picks_nothing() -> None:
    idx = index(["food", "rust", "python", "music", "cars", "film", "art", "rust-2"])
    texts = [" ".join(AXES)]  # vague: the model alone picks nothing
    assert feed_topics(texts, fake_embed, idx) == []
    assert feed_topics(texts * 2, fake_embed, idx, [["cars"], ["cars"]]) == ["cars"]


def test_about_text_needs_a_subtitle_of_its_own() -> None:
    assert (
        about_text("Food in Jars", "A blog about canning.") == "Food in Jars. A blog about canning"
    )
    assert about_text("My Site", "Just another WordPress site") is None  # boilerplate only
    assert about_text("Max", "Recent content in Max's Webpage on Max") is None
    assert about_text("Feep! blog", None) is None  # a name alone
    assert about_text("PinePods Docs Blog", "PinePods Docs Blog") is None  # the title again
    assert about_text("mrcoles.com feed", "mrcoles.com posts feed") is None
    assert (
        about_text(None, "Notes on machine learning and data")
        == "Notes on machine learning and data"
    )
    assert about_text(None, None) is None


def test_the_feed_description_counts_like_several_items() -> None:
    idx = index(["food", "rust", "python", "music", "cars", "film", "art"])
    texts = ["rust ownership", "python packaging"]  # rust and python tie
    tied = feed_topics(texts, fake_embed, idx)
    assert set(tied) == {"rust", "python"}
    assert ABOUT_WEIGHT > 1
    assert feed_topics(texts, fake_embed, idx, about="all about rust") == ["rust"]


def test_topic_model_takes_plain_posts() -> None:
    model = TopicModel(
        index(["food", "rust", "python", "music", "cars", "film", "art"]), fake_embed
    )
    posts = [Post("rust ownership", ("Python",)), Post("rust and python"), Post("rust and python")]
    assert model.topics(posts) == ["rust"]
    posts = [Post("rust ownership", ("Python",)), Post("rust and python", ("python",)), posts[2]]
    assert model.topics(posts) == ["rust", "python"]  # the authors' tags back python
    # a feed description all about rust pulls the feed away from python: no longer plausible
    assert model.topics(posts, "Ferris", "All about rust and nothing else") == ["rust"]
    assert model.topics([]) == []


def test_build_index_then_load_it(tmp_path: Path) -> None:
    model, tokenizer = tmp_path / "m.onnx", tmp_path / "t.json"
    model.write_bytes(b"model")
    tokenizer.write_bytes(b"tokenizer")
    tags = tmp_path / "tags"
    tags.mkdir()
    (tags / "topics.txt").write_text("rust\nfood\n\npython-packaging\n")
    (tags / "synonyms.tsv").write_text("")
    seen: list[str] = []

    def embed(texts: Sequence[str]) -> Vectors:
        seen.extend(texts)
        return fake_embed(texts)

    assert build_index(tags, model, tokenizer, embed) == 3
    assert seen == ["rust", "food", "python packaging"]  # words, in file order
    idx = load_index(tags, model, tokenizer)  # the hashes match: not stale
    assert idx.names == ("rust", "food", "python-packaging") and idx.matrix.shape == (3, 8)
    assert np.allclose(idx.matrix, fake_embed(seen), atol=1e-3)  # float16
    assert name_text("web_dev-tools") == "web dev tools"


def test_build_index_refuses_an_empty_list(tmp_path: Path) -> None:
    (tmp_path / "topics.txt").write_text("\n")
    with pytest.raises(TopicsError, match="no topic names"):
        build_index(tmp_path, tmp_path / "m", tmp_path / "t", fake_embed)


def test_the_authors_first_label_when_nothing_else_picks_a_topic() -> None:
    idx = index(["food", "rust", "python", "music", "cars", "film", "art", "rust-2"])
    vague = [" ".join(AXES)] * 3  # the model alone picks nothing
    # the newest item with labels decides, by its first label only; implausible or not
    assert feed_topics(vague, fake_embed, idx, [[], ["Music", "food"], ["cars"]]) == ["music"]
    # its first label names no topic: nothing (the next label and older items are not tried)
    assert feed_topics(vague, fake_embed, idx, [["journal", "food"], ["cars"]]) == []
    assert feed_topics(vague, fake_embed, idx, [[], []]) == []
    # a post without usable text still has its labels
    assert first_author_topic([["Film"]], idx) == ["film"]
    assert feed_topics(["  "], fake_embed, idx, [["Film"]]) == ["film"]


def test_the_first_label_is_not_used_when_the_model_picks_a_topic() -> None:
    idx = index(["food", "rust", "python", "music", "cars", "film", "art"])
    assert feed_topics(["rust ownership", "rust lifetimes"], fake_embed, idx, [["food"]]) == [
        "rust"
    ]
