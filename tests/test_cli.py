"""Argument parsing for the indexer commands."""

import logging
from datetime import UTC, datetime

import pytest

from indexer.check.runner import CheckRow
from indexer.cli import _log_failure, _parser, _print_failures, _sources_exit_code


def test_run_from_file() -> None:
    args = _parser().parse_args(["run", "data/urls.txt"])
    assert str(args.file) == "data/urls.txt" and not args.from_inputs


def test_run_from_inputs_defaults() -> None:
    args = _parser().parse_args(["run", "--from-inputs"])
    assert args.from_inputs and args.file is None
    assert (args.batch, args.stale_days) == (500, 14)


@pytest.mark.parametrize("argv", [["run"], ["run", "urls.txt", "--from-inputs"]])
def test_run_needs_exactly_one_source(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(argv)


def test_skipped_source_is_a_warning_not_a_failure() -> None:
    assert _sources_exit_code({"a": "404"}, total=7) == 0
    assert _sources_exit_code({}, total=7) == 0
    assert _sources_exit_code({str(i): "x" for i in range(3)}, total=3) == 1  # nothing loaded


def test_github_actions_warning_annotation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _print_failures({"submissions": "https://x.example/a: http_4xx 404\n100%"})
    out, err = capsys.readouterr()
    assert out == (
        "::warning title=Source skipped::submissions: https://x.example/a: http_4xx 404%0A100%25\n"
    )
    assert "SKIPPED" in err


def test_no_annotation_outside_actions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    _print_failures({"a": "gone"})
    out, err = capsys.readouterr()
    assert out == "" and "a" in err


def test_verbose_logs_failures_only(caplog: pytest.LogCaptureFixture) -> None:
    now = datetime.now(UTC)
    caplog.set_level(logging.INFO, logger="indexer")
    _log_failure(CheckRow(url="https://ok.example/feed", checked_at=now, item_count=3))
    assert caplog.text == ""
    _log_failure(
        CheckRow(
            url="https://bad.example/feed",
            checked_at=now,
            error="not_a_feed",
            detail="HTML document",
        )
    )
    assert "not_a_feed" in caplog.text and "https://bad.example/feed" in caplog.text
    assert "HTML document" in caplog.text


def test_cleanup_refuses_to_cut_into_the_export_window(capsys: pytest.CaptureFixture[str]) -> None:
    from indexer.cli import main

    assert main(["cleanup", "--months", "3", "--database-url", "postgresql://unused"]) == 2
    assert "at least 7" in capsys.readouterr().err


def test_survivors_defaults() -> None:
    args = _parser().parse_args(["survivors"])
    assert (args.years_ago, args.slice_mb, args.active_days, args.max_feeds) == (5, 25, 243, 50)
    assert str(args.config) == "data/sources.toml"
    assert str(args.file) == "data/survivors.txt" and args.report is None


def test_drop_tables_needs_yes(capsys: pytest.CaptureFixture[str]) -> None:
    from indexer.cli import main

    assert main(["drop-tables", "--database-url", "postgresql://unused"]) == 2
    assert "pass --yes" in capsys.readouterr().err
