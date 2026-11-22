"""The languages of a feed, from plain text: no RSS parsing, no database.

    model = LanguageModel.load()  # fastText lid.176 from the environment
    model.post_languages(["Title. Opening...", ...])  # -> ["en", "de", None, ...], one per post
    model.languages(["Title. Opening...", ...])  # -> ["de", "en"], the feed's labels

Each post's text (title and the start of its description) goes through fastText's language
identification model lid.176 (176 languages; by Facebook AI Research, licensed CC BY-SA 3.0:
https://fasttext.cc/docs/en/language-identification.html). Posts shorter than MIN_TEXT_LEN are
not judged. A language becomes one of the feed's labels when it is detected on at least MIN_ITEMS
posts and MIN_SHARE of the judged posts, so one misread title does not label a feed. A feed in
several languages gets several labels (ISO 639 codes, sorted); one with no post long enough gets
none.
"""

import os
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

MODEL_ENV = "INDEXER_LID_MODEL"  # path of lid.176.ftz; the Docker image sets it
DEFAULT_MODEL = Path("/opt/lid.176.ftz")
MIN_TEXT_LEN = 40  # characters: shorter post texts are skipped, too little to tell
MIN_ITEMS = 2  # a language needs this many posts...
MIN_SHARE = 0.2  # ...and this share of the posts that were judged
_CODE = re.compile(r"[a-z]{2,3}")

# Text -> ISO 639 code ("en"), or None when nothing is detected.
Detect = Callable[[str], str | None]


def load_detector(path: Path | None = None) -> Detect:
    """The fastText detector, loaded from `path`, else $INDEXER_LID_MODEL, else DEFAULT_MODEL."""
    import fasttext  # type: ignore[import-untyped]  # fasttext-predict ships no type hints

    model = fasttext.load_model(str(path or os.environ.get(MODEL_ENV) or DEFAULT_MODEL))

    def detect(text: str) -> str | None:
        labels, _scores = model.predict(" ".join(text.split()), k=1)  # one line, as it expects
        return str(labels[0]).removeprefix("__label__") if labels else None

    return detect


def post_languages(texts: Iterable[str], detect: Detect) -> list[str | None]:
    """The language of each text, in order: None when it is shorter than MIN_TEXT_LEN (not
    judged), "" when judged but nothing usable was detected."""
    out: list[str | None] = []
    for text in texts:
        if len(text.strip()) < MIN_TEXT_LEN:
            out.append(None)
            continue
        code = detect(text)
        out.append(code if code is not None and _CODE.fullmatch(code) else "")
    return out


def feed_languages(per_post: Iterable[str | None]) -> list[str]:
    """The ISO 639 code of each language on at least MIN_ITEMS and MIN_SHARE of the judged posts
    (see `post_languages`), sorted."""
    judged = [code for code in per_post if code is not None]
    counts = Counter(code for code in judged if code)
    return sorted(c for c, n in counts.items() if n >= MIN_ITEMS and n >= MIN_SHARE * len(judged))


class LanguageModel:
    """fastText lid.176, loaded once."""

    def __init__(self, detect: Detect) -> None:
        self.detect = detect

    @classmethod
    def load(cls, path: Path | None = None) -> "LanguageModel":
        """The model at `path`, else $INDEXER_LID_MODEL, else the image's default."""
        return cls(load_detector(path))

    def post_languages(self, texts: Sequence[str]) -> list[str | None]:
        """Each post's language (see `post_languages`)."""
        return post_languages(texts, self.detect)

    def languages(self, texts: Sequence[str]) -> list[str]:
        """The feed's language labels (see `feed_languages`)."""
        return feed_languages(self.post_languages(texts))
