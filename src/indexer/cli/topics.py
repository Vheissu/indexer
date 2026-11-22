"""indexer topics: the topics of some text (no feed, no database), to try the topic model."""

import argparse

from indexer.cli.common import topic_model
from indexer.topics import Post

NAME = "topics"
HELP = (
    "print the topics of some posts (each TEXT is one post: its title and opening), one per"
    " line, most similar first; nothing is fetched or stored"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("text", nargs="+", metavar="TEXT", help="a post's text, in English")
    parser.add_argument("--title", help="the feed's title")
    parser.add_argument("--subtitle", help="the feed's subtitle (counts only if it says something)")
    parser.add_argument(
        "--tags",
        action="append",
        default=[],
        metavar="A,B",
        help="one post's tags, comma separated; repeat in the order of the TEXTs",
    )


async def run(args: argparse.Namespace) -> int:
    if (model := topic_model()) is None:
        return 2
    tags = [[t.strip() for t in group.split(",") if t.strip()] for group in args.tags]
    posts = [Post(text, tags[i] if i < len(tags) else ()) for i, text in enumerate(args.text)]
    for topic in model.topics(posts, args.title, args.subtitle):
        print(topic)
    return 0
