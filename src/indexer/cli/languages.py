"""indexer languages: the languages of some text (no feed, no database), to try the model."""

import argparse

from indexer.cli.common import language_model

NAME = "languages"
HELP = (
    "print the language of each TEXT (one post each: its title and opening), then the feed's"
    " labels; nothing is fetched or stored"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("text", nargs="+", metavar="TEXT", help="a post's text")


async def run(args: argparse.Namespace) -> int:
    if (model := language_model()) is None:
        return 2
    for text, code in zip(args.text, model.post_languages(args.text), strict=True):
        shown = "(too short)" if code is None else code or "(unknown)"
        print(f"{shown:12} {' '.join(text.split())[:70]}")
    print(f"feed: {', '.join(model.languages(args.text)) or '(none)'}")
    return 0
