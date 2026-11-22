"""Language labels from per-item detection. A fake detector stands in for the fastText model
(tests never need the model file or the network)."""

import pytest

from indexer.text.language import MIN_TEXT_LEN, LanguageModel


def fake(text: str) -> str | None:
    """The language is the text's first word ("fr ..." -> "fr"); "?" detects nothing."""
    word = text.split()[0]
    return None if word == "?" else word


def texts(*codes: str) -> list[str]:
    return [f"{code} " + "x" * MIN_TEXT_LEN for code in codes]


def test_one_language() -> None:
    assert MODEL.languages(texts("en", "en", "en")) == ["en"]


def test_several_languages_each_get_a_label_sorted() -> None:
    assert MODEL.languages(texts("fr", "en", "fr", "en", "en")) == ["en", "fr"]


def test_a_language_on_one_item_only_is_not_a_label() -> None:
    assert MODEL.languages(texts("en", "en", "en", "ur")) == ["en"]


def test_a_language_below_a_fifth_of_the_items_is_not_a_label() -> None:
    # 2 of 11 (18%): enough items, too small a share
    assert MODEL.languages(texts(*["en"] * 9, "de", "de")) == ["en"]
    assert MODEL.languages(texts(*["en"] * 8, "de", "de")) == ["de", "en"]  # 2 of 10


def test_short_texts_are_not_judged() -> None:
    short = ["de short"] * 5  # under MIN_TEXT_LEN: skipped, and not counted in the share
    assert MODEL.languages([*short, *texts("en", "en")]) == ["en"]
    assert MODEL.languages(short) == []


def test_nothing_detected_or_no_items() -> None:
    assert MODEL.languages(texts("?", "?")) == []
    assert MODEL.languages([]) == []


@pytest.mark.parametrize("code", ["EN", "e", "english", "en-us", "../x", "en,fr"])
def test_odd_codes_from_the_detector_are_ignored(code: str) -> None:
    assert MODEL.languages(texts(code, code, code)) == []


def test_post_languages_one_per_post() -> None:
    posts = [*texts("en", "?"), "too short"]
    assert MODEL.post_languages(posts) == ["en", "", None]  # judged, nothing usable, not judged


MODEL = LanguageModel(fake)
