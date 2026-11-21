"""The proposal filter: examples from reviewed proposal files, the trained model on disk, its
scores and cut-off."""

import re
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from indexer.check.feed import RecentPost
from indexer.survivors.proposals import render_entries
from indexer.survivors.run import Survivor
from indexer.training import (
    TrainingError,
    load_model,
    model_paths,
    parse_proposals,
    reviewed_text,
    tokens,
    train_model,
    training_files,
)

WAT = "https://data.commoncrawl.org/crawl-data/CC-MAIN-2021-49/segments/1/wat/x.warc.wat.gz"
GOOD = Survivor(
    "https://good.example/feed/",
    WAT,
    "Miss Frugal Mommy",
    "en",
    datetime(2026, 11, 12, tzinfo=UTC),
    subtitle="Saving money with kids",
    recent_posts=(RecentPost("https://good.example/p/", "Ten ways to save", "We spend..."),),
    categories=(("budgeting", 12),),
    posts_read=30,
)
BAD = replace(GOOD, url="https://bad.example/feed", title="Casino Deals", recent_posts=())


def reviewed(*survivors: Survivor, accept: str = GOOD.url) -> str:
    text = render_entries(survivors, "CC-MAIN-2021-49", date(2026, 11, 21))
    return text.replace(f"# {accept}\n", f"{accept}\n")


def test_parse_proposals_gives_label_and_reviewed_text() -> None:
    good, bad = parse_proposals(reviewed(GOOD, BAD))
    assert (good.url, good.accepted) == ("https://good.example/feed/", True)
    assert (bad.url, bad.accepted) == ("https://bad.example/feed", False)
    assert good.text == (
        "Miss Frugal Mommy about: Saving money with kids categories: budgeting 12"
        " Ten ways to save We spend..."
    )  # no languages, date, WAT file or post link
    assert bad.text == "Casino Deals about: Saving money with kids categories: budgeting 12"


def test_a_rejection_reason_is_read_and_not_part_of_the_text() -> None:
    text = reviewed(GOOD, BAD).replace(f"# {BAD.url}\n", f"# rejected: unsafe\n# {BAD.url}\n")
    good, bad = parse_proposals(text)
    assert (good.reason, bad.reason) == (None, "unsafe")
    assert bad.text == parse_proposals(reviewed(GOOD, BAD))[1].text


@pytest.mark.parametrize(
    ("line", "url"), [("# rejected: boring", BAD.url), ("# rejected: seo", GOOD.url)]
)
def test_an_unknown_reason_or_a_reason_on_an_accepted_feed_fails(line: str, url: str) -> None:
    text = reviewed(GOOD, BAD)
    target = f"{url}\n" if url == GOOD.url else f"# {url}\n"
    with pytest.raises(TrainingError):
        parse_proposals(text.replace(target, f"{line}\n{target}"))


def test_a_score_line_is_not_part_of_the_text() -> None:
    (scored,) = parse_proposals(reviewed(replace(GOOD, score=0.031)))
    assert "# score: 0.031" in reviewed(replace(GOOD, score=0.031))
    assert scored.text == parse_proposals(reviewed(GOOD))[0].text


def test_reviewed_text_handles_a_wrapped_summary() -> None:
    joined = "Midnight Sun Gun Dog Association · en · last post 2026-10-08 about: Our Dogs Hunt!"
    assert reviewed_text(joined) == "Midnight Sun Gun Dog Association about: Our Dogs Hunt!"


def test_tokens_are_words_and_word_pairs() -> None:
    assert tokens("Press Release: 10 Best!") == [
        "press",
        "release",
        "10",
        "best",
        "press_release",
        "release_10",
        "10_best",
    ]


def labelled_file(n: int) -> str:
    """n frugal blogs (kept) and n casino pages (rejected as unsafe), each a little different."""
    good = [
        replace(GOOD, url=f"https://good{i}.example/feed", title=f"Frugal mom diary {i}")
        for i in range(n)
    ]
    bad = [
        replace(
            BAD,
            url=f"https://bad{i}.example/feed",
            title=f"Casino bonus slots {i}",
            subtitle="Best casino bonus codes",
        )
        for i in range(n)
    ]
    text = render_entries([*good, *bad], "CC-MAIN-2021-49", date(2026, 11, 21))
    for s in good:
        text = text.replace(f"# {s.url}\n", f"{s.url}\n")
    for s in bad:
        text = text.replace(f"# {s.url}\n", f"# rejected: unsafe\n# {s.url}\n")
    return text


def test_train_store_load_and_score(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text(labelled_file(10))
    config = tmp_path / "training.toml"
    config.write_text('files = ["a.txt"]\n')
    out = tmp_path / "models"
    trained = train_model(config, out)
    assert sorted(p.name for p in out.iterdir()) == ["proposals.json", "proposals.npz"]
    model = load_model(config, out)
    assert model.vocab == trained.vocab and model.threshold == trained.threshold
    good, bad = model.scores(["Frugal mom diary: saving money with kids", "Casino bonus slots"])
    assert good > 0.5 > bad and good > model.threshold
    assert model.scores([]) == []
    assert train_model(config, out).threshold == trained.threshold  # deterministic


def test_the_cut_drops_rejects_and_keeps_accepted_feeds(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text(labelled_file(10))
    (tmp_path / "training.toml").write_text('files = ["a.txt"]\n')
    model = train_model(tmp_path / "training.toml", tmp_path / "models")
    examples = parse_proposals(labelled_file(10))
    scores = model.scores([p.text for p in examples])
    dropped = [p for p, s in zip(examples, scores, strict=True) if s < model.threshold]
    assert dropped and not any(p.accepted for p in dropped)


def test_a_changed_review_makes_the_model_stale(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text(labelled_file(3))
    config = tmp_path / "training.toml"
    config.write_text('files = ["a.txt"]\n')
    train_model(config, tmp_path / "models")
    (tmp_path / "a.txt").write_text(labelled_file(3).replace("# rejected: unsafe\n", "", 1))
    with pytest.raises(TrainingError, match="stale"):
        load_model(config, tmp_path / "models")


def test_both_labels_are_needed(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text(reviewed(GOOD))  # one feed, accepted
    (tmp_path / "training.toml").write_text('files = ["a.txt"]\n')
    with pytest.raises(TrainingError, match="both"):
        train_model(tmp_path / "training.toml", tmp_path / "models")
    assert not model_paths(tmp_path / "models")[0].exists()


def test_a_config_without_files_is_refused(tmp_path: Path) -> None:
    (tmp_path / "training.toml").write_text('files = "a.txt"\n')
    with pytest.raises(TrainingError, match="list of paths"):
        training_files(tmp_path / "training.toml")


def test_rejection_reasons_do_not_change_the_model(tmp_path: Path) -> None:  # two classes
    text = labelled_file(10)
    assert "# rejected: " in text
    models = []
    for name, body in (("with", text), ("without", re.sub(r"# rejected: \S+\n", "", text))):
        (tmp_path / f"{name}.txt").write_text(body)
        (tmp_path / f"{name}.toml").write_text(f'files = ["{name}.txt"]\n')
        models.append(train_model(tmp_path / f"{name}.toml", tmp_path / name))
    assert models[0].weights.shape[1] == 2
    assert (models[0].weights == models[1].weights).all()
    assert models[0].threshold == models[1].threshold
