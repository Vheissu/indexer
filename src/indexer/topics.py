"""The topics of a feed, from plain text: no RSS parsing, no language detection, no database.

    model = TopicModel.load()  # the topic files in data/topics, MiniLM from the environment
    model.topics([Post("Async Rust without the pain. Futures...", ("rust",)), ...],
                 title="...", subtitle="...")  # -> ["rust", "async"]

The caller chooses the posts (the checker passes the English ones: the topic names and MiniLM
are English) and gives each one's text (title and the start of its description) and, optionally,
its tags as the author wrote them. Each post's text is embedded with MiniLM (all-MiniLM-L6-v2,
quantized ONNX; Apache-2.0), and so are the feed's title and subtitle when the subtitle says
something of its own (`about_text`); they are averaged into one vector (the feed text counting
like ABOUT_WEIGHT posts), which is compared with the precomputed embedding of every topic name in
data/topics/topics.txt (data/topics/topics.npy, one row per line, float16, unit length). Names are
mapped to their canonical form with data/topics/synonyms.tsv (two names for one topic count once);
see `feed_topics` for which are kept. Authors' tags are evidence (`author_topics`), and the
first one stands in when nothing else gives a topic (`first_author_topic`).
Inputs and thresholds were tuned by hand on random listed feeds (2026-11-18), precision first:
about 85% of the topics right, on about half of the English feeds.

data/topics/topics.json records the SHA-256 of topics.txt, the model and the tokenizer the matrix
was built from: loading refuses a matrix that no longer matches them. `build_index` (`indexer
embeddings`, the embeddings workflow) rebuilds both from topics.txt.
"""

import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import numpy.typing as npt

MODEL_ENV = "INDEXER_MINILM_MODEL"  # the Docker image sets both
TOKENIZER_ENV = "INDEXER_MINILM_TOKENIZER"
DEFAULT_MODEL = Path("/opt/minilm.onnx")
DEFAULT_TOKENIZER = Path("/opt/minilm-tokenizer.json")
TOPICS_DIR = Path("data/topics")  # topics.txt, topics.npy, topics.json, synonyms.tsv
TOP = 5  # topics per feed, at most
MIN_SCORE = 0.35  # cosine similarity between the texts' average and the topic name, at least
MARGIN = 0.02  # a kept topic scores within this of the feed's best
SPREAD_RANK = 8  # the feed's best must beat its 8th best distinct name...
MIN_SPREAD = 0.08  # ...by this much, or the model picks nothing
# Authors' tags are evidence, never stored: a topic that the tags name (after spelling folds and
# synonyms) on at least TAG_MIN_ITEMS items and TAG_MIN_SHARE of the English items is added after
# the model's picks if the model finds it plausible (scores at least AUTHOR_MIN_SCORE), up to TOP.
# Items with more than MAX_ITEM_TAGS tags are not counted (stuffing). Bookkeeping tags ("journal",
# "personal", "essay") score low against the posts and are left out by that floor.
AUTHOR_MIN_SCORE = 0.35
TAG_MIN_ITEMS = 2
TAG_MIN_SHARE = 0.2
MAX_ITEM_TAGS = 10
# The feed's own title and subtitle ("Food in Jars. A blog about canning...") count like
# ABOUT_WEIGHT items, once boilerplate ("Just another WordPress site") is removed.
ABOUT_WEIGHT = 3.0
_BOILERPLATE = re.compile(
    r"just another wordpress site|recent content (?:in|on)\b.*|the latest articles on\b.*"
    r"|\b(?:rss|atom|posts?) feed\b|\bwelcome to\b",
    re.IGNORECASE,
)
MAX_TOKENS = 128  # per item text (up to 300 characters, about 70 tokens)

Vectors = npt.NDArray[np.float32]
# Texts -> one unit-length row per text.
Embed = Callable[[Sequence[str]], Vectors]
_NOT_KEY = re.compile(r"[^a-z0-9+#]")


class TopicsError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Post:
    text: str  # title and the start of the description, plain text
    tags: Sequence[str] = ()  # the author's tags, as written (evidence only)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def read_synonyms(path: Path) -> dict[str, str]:
    """variant -> canonical, with chains resolved (a -> b -> c gives a -> c)."""
    pairs: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            variant, _, canonical = line.partition("\t")
            if variant.strip() and canonical.strip():
                pairs[variant.strip()] = canonical.strip()
    resolved: dict[str, str] = {}
    for variant in pairs:
        name, seen = variant, {variant}
        while name in pairs and pairs[name] not in seen:
            name = pairs[name]
            seen.add(name)
        resolved[variant] = name
    return resolved


def tag_key(tag: str) -> str:
    """A tag or topic name reduced for matching: lower-case ASCII letters, digits, + and #, with
    a leading hashtag sign dropped ("#AI" -> "ai", "Web Dev" -> "webdev", "C++" -> "c++")."""
    folded = unicodedata.normalize("NFKD", tag.strip().lstrip("#")).encode("ascii", "ignore")
    return _NOT_KEY.sub("", folded.decode("ascii").lower())


@dataclass(frozen=True, slots=True)
class TopicIndex:
    names: tuple[str, ...]  # topics.txt, in file order
    matrix: Vectors  # one unit-length row per name
    synonyms: dict[str, str]  # variant -> canonical
    canonical: tuple[str, ...] = field(init=False)  # each row's canonical name
    by_key: dict[str, str] = field(init=False)  # tag_key -> canonical name
    rows: dict[str, tuple[int, ...]] = field(init=False)  # canonical name -> its rows

    def __post_init__(self) -> None:
        canonical = tuple(self.synonyms.get(n, n) for n in self.names)
        by_key: dict[str, str] = {}
        for name, canon in zip(self.names, canonical, strict=True):
            by_key.setdefault(tag_key(name), canon)  # names are most used first
        rows: dict[str, list[int]] = {}
        for i, canon in enumerate(canonical):
            rows.setdefault(canon, []).append(i)
        object.__setattr__(self, "canonical", canonical)
        object.__setattr__(self, "by_key", by_key)
        object.__setattr__(self, "rows", {name: tuple(r) for name, r in rows.items()})

    def topic_of(self, tag: str) -> str | None:
        """The canonical topic an author's tag names, if any (a plural or singular form too)."""
        key = tag_key(tag)
        if not key:
            return None
        for k in (key, key[:-1] if key.endswith("s") else key + "s"):
            if k in self.by_key:
                return self.by_key[k]
        return None


def author_topics(item_tags: Sequence[Sequence[str]], index: TopicIndex) -> set[str]:
    """The topics the authors' tags name on at least TAG_MIN_ITEMS items and TAG_MIN_SHARE of
    all the items; an item with more than MAX_ITEM_TAGS tags counts in the share, not the tags."""
    counts: Counter[str] = Counter()
    for tags in item_tags:
        if len(tags) <= MAX_ITEM_TAGS:
            counts.update({t for tag in tags if (t := index.topic_of(tag))})
    need = max(TAG_MIN_ITEMS, TAG_MIN_SHARE * len(item_tags))
    return {topic for topic, n in counts.items() if n >= need}


def load_index(directory: Path, model: Path, tokenizer: Path) -> TopicIndex:
    """The topic names, their matrix and the synonyms; TopicsError if the matrix was built from
    another topics.txt, model or tokenizer, or doesn't have one row per name."""
    names_path = directory / "topics.txt"
    names = tuple(n for n in names_path.read_text(encoding="utf-8").splitlines() if n.strip())
    meta = json.loads((directory / "topics.json").read_text(encoding="utf-8"))
    for key, path in (
        ("topics_sha256", names_path),
        ("model_sha256", model),
        ("tokenizer_sha256", tokenizer),
    ):
        if meta.get(key) != sha256(path):
            raise TopicsError(
                f"{directory / 'topics.npy'} is stale: {path} changed since it was built"
            )
    matrix = np.load(directory / "topics.npy").astype(np.float32)
    if matrix.ndim != 2 or matrix.shape[0] != len(names):
        raise TopicsError(f"topics.npy has shape {matrix.shape}, expected {len(names)} rows")
    return TopicIndex(names, matrix, read_synonyms(directory / "synonyms.tsv"))


def name_text(name: str) -> str:
    """A topic name as MiniLM reads it: words, not a slug ("web-development" -> "web
    development")."""
    return name.replace("-", " ").replace("_", " ")


def model_paths(model: Path | None, tokenizer: Path | None) -> tuple[Path, Path]:
    """MiniLM's files: as given, else $INDEXER_MINILM_MODEL and $INDEXER_MINILM_TOKENIZER, else
    the image's defaults."""
    return (
        model or Path(os.environ.get(MODEL_ENV) or DEFAULT_MODEL),
        tokenizer or Path(os.environ.get(TOKENIZER_ENV) or DEFAULT_TOKENIZER),
    )


def build_index(
    directory: Path = TOPICS_DIR,
    model: Path | None = None,
    tokenizer: Path | None = None,
    embed: Embed | None = None,
) -> int:
    """Embed every name in `directory`/topics.txt (in file order) and write topics.npy (float16,
    unit length, one row per name) and topics.json (the hashes `load_index` checks). `embed`
    defaults to MiniLM (see `model_paths`). Returns the number of names."""
    model, tokenizer = model_paths(model, tokenizer)
    names_path = directory / "topics.txt"
    names = [n for n in names_path.read_text(encoding="utf-8").splitlines() if n.strip()]
    if not names:
        raise TopicsError(f"{names_path} has no topic names")
    embed = embed or load_embedder(model, tokenizer)
    texts = [name_text(n) for n in names]
    matrix = np.vstack([embed(texts[i : i + 256]) for i in range(0, len(texts), 256)])
    np.save(directory / "topics.npy", matrix.astype(np.float16))
    meta = {
        "rows": len(names),
        "dim": int(matrix.shape[1]),
        "dtype": "float16",
        "topics_sha256": sha256(names_path),
        "model_sha256": sha256(model),
        "tokenizer_sha256": sha256(tokenizer),
        "model": "Xenova/all-MiniLM-L6-v2 (quantized)",
    }
    (directory / "topics.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return len(names)


def about_text(title: str | None, subtitle: str | None) -> str | None:
    """The feed's title and subtitle as one text, boilerplate removed; None unless the subtitle
    says something of its own (at least 3 words, not just the title again): a feed's name alone
    dilutes the posts more than it helps ("Feep! blog", "PinePods Docs Blog")."""

    def clean(text: str | None) -> str:
        return " ".join(_BOILERPLATE.sub(" ", text or "").split()).strip(" .-|:")

    about, name = clean(subtitle), clean(title)
    if len(about.split()) < 3 or about.casefold() in name.casefold():
        return None
    return f"{name}. {about}" if name and name.casefold() not in about.casefold() else about


def feed_topics(
    texts: Sequence[str],
    embed: Embed,
    index: TopicIndex,
    item_tags: Sequence[Sequence[str]] = (),
    about: str | None = None,
    first_label: bool = True,
) -> list[str]:
    """The topics of a feed from its posts' text, most similar first; empty without text or
    when no topic stands out.

    The texts' average is scored against every topic name, and names are mapped to their
    canonical form (synonyms.tsv). The model picks topics only if its best name beats its
    SPREAD_RANK-th best by MIN_SPREAD (a vague feed scores many unrelated names about equally),
    then up to TOP names scoring within MARGIN of the best and at least MIN_SCORE. Topics the
    authors' tags back (`author_topics`) follow, if they score at least AUTHOR_MIN_SCORE, up to
    TOP in all. `about` (the feed's title and subtitle, see `about_text`) counts like
    ABOUT_WEIGHT items. If all that picks nothing and `first_label` is true, the author's first
    label stands in (`first_author_topic`)."""
    picked = _scored_topics(texts, embed, index, item_tags, about)
    return picked or (first_author_topic(item_tags, index) if first_label else [])


def first_author_topic(item_tags: Sequence[Sequence[str]], index: TopicIndex) -> list[str]:
    """The fallback for a feed nothing else gives a topic: the first label of the newest item
    that has labels (items newest first), if it names a topic; no other label is tried. A wrong
    one is at least the author's own choice (agreed 2026-11-18; about 40-45% right on a hand-judged
    sample, against ~75% for the model's picks)."""
    for tags in item_tags:
        if tags:
            topic = index.topic_of(tags[0])
            return [topic] if topic else []
    return []


def _scored_topics(
    texts: Sequence[str],
    embed: Embed,
    index: TopicIndex,
    item_tags: Sequence[Sequence[str]],
    about: str | None,
) -> list[str]:
    texts = [t for t in texts if t.strip()]
    if not texts:
        return []
    vectors = embed([*texts, about] if about else texts)
    weights = np.ones(len(vectors), dtype=np.float32)
    if about:
        weights[-1] = ABOUT_WEIGHT  # the feed's own description of itself
    centre = (vectors * weights[:, None]).sum(axis=0)
    norm = float(np.linalg.norm(centre))
    if norm == 0.0:
        return []
    scores = index.matrix @ (centre / norm)
    picked = _model_topics(scores, index)
    backed = author_topics(item_tags, index)
    plausible = {name: float(scores[list(index.rows[name])].max()) for name in backed}
    for name in sorted(plausible, key=lambda n: (-plausible[n], n)):
        if len(picked) == TOP:
            break
        if plausible[name] >= AUTHOR_MIN_SCORE and name not in picked:
            picked.append(name)
    return picked


def _model_topics(scores: Vectors, index: TopicIndex) -> list[str]:
    """The model's own picks: nothing unless the best name beats the SPREAD_RANK-th by
    MIN_SPREAD, then up to TOP names within MARGIN of the best and at least MIN_SCORE."""
    ranked: list[tuple[str, float]] = []  # distinct canonical names, best first
    for i in np.argsort(-scores):
        name = index.canonical[int(i)]
        if all(name != seen for seen, _ in ranked):
            ranked.append((name, float(scores[i])))
            if len(ranked) == SPREAD_RANK:
                break
    best = ranked[0][1]
    if best - ranked[-1][1] < MIN_SPREAD:
        return []
    floor = max(MIN_SCORE, best - MARGIN)
    return [name for name, score in ranked[:TOP] if score >= floor]


class TopicModel:
    """MiniLM and the topic files, loaded once; `topics` is safe to call from several threads."""

    def __init__(self, index: TopicIndex, embed: Embed) -> None:
        self.index = index
        self.embed = embed

    @classmethod
    def load(
        cls,
        directory: Path = TOPICS_DIR,
        model: Path | None = None,
        tokenizer: Path | None = None,
    ) -> "TopicModel":
        """The topic files in `directory`; MiniLM from `model` and `tokenizer`, else
        $INDEXER_MINILM_MODEL and $INDEXER_MINILM_TOKENIZER, else the image's defaults.
        TopicsError if the matrix is stale (see `load_index`)."""
        model, tokenizer = model_paths(model, tokenizer)
        return cls(load_index(directory, model, tokenizer), load_embedder(model, tokenizer))

    def topics(
        self,
        posts: Sequence[Post],
        title: str | None = None,
        subtitle: str | None = None,
        *,
        first_label: bool = True,
    ) -> list[str]:
        """The feed's topics, most similar first (at most TOP; empty if none stands out).
        `first_label`: fall back on the author's first label (see `feed_topics`)."""
        return feed_topics(
            [p.text for p in posts],
            self.embed,
            self.index,
            [tuple(p.tags) for p in posts],
            about_text(title, subtitle),
            first_label,
        )
