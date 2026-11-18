# Feedrule Indexer

Thousands of RSS and Atom feeds, sorted by topic, that are **still alive**. Every feed is
re-checked about every two weeks, and only feeds that work and have posted in recent years are
listed. Dead feeds drop off on their own; nobody prunes the list by hand.

**[Browse the feeds by topic →](https://feedrule.github.io/indexer/)**

A few popular topics: [ai](https://feedrule.github.io/indexer/topic/ai/) ·
[security](https://feedrule.github.io/indexer/topic/security/) ·
[javascript](https://feedrule.github.io/indexer/topic/javascript/) ·
[programming](https://feedrule.github.io/indexer/topic/programming/) ·
[python](https://feedrule.github.io/indexer/topic/python/) ·
[linux](https://feedrule.github.io/indexer/topic/linux/) ·
[books](https://feedrule.github.io/indexer/topic/books/) ·
[writing](https://feedrule.github.io/indexer/topic/writing/) ·
[music](https://feedrule.github.io/indexer/topic/music/) ·
[design](https://feedrule.github.io/indexer/topic/design/) ·
[travel](https://feedrule.github.io/indexer/topic/travel/)

This is the open-source index behind [feedrule.com](https://feedrule.com/search), where you can
search these feeds and get any of them by email. This repository holds the code that finds and
checks the feeds, and the published lists themselves.

## What you get for each topic

Every topic has a page on the site, `https://feedrule.github.io/indexer/topic/<topic>/`, with up to 200 feeds (title, website,
date of the last post), most recently active first. The icon next to each feed opens
[feedrule.com](https://feedrule.com/) with that feed filled in, to get its new posts by email.

The data behind the pages is on the `output` branch, in `topic/<topic>/`:

| file | what it is |
| --- | --- |
| `feeds.opml` | the 500 most recently active feeds, to import into a feed reader in one go |
| `links.txt` | every feed in the topic (up to 50,000), one URL per line, most recently active first, for scripts and tools |

`topic/topics.txt` lists the topic names, one per line, most feeds first. In folder names, `+` and
`#` are spelled out: `c++` is in `topic/cplusplus/`, `c#` in `topic/csharp/`. A feed none of whose
topics stands out is in no topic. **Every** listed feed, with a topic or not, is in
the `all/` folder: `all/links.txt` (one URL per line) and `all/feeds.opml`. Each listed feed also
has a small JSON preview (URL, title, description, size in bytes, last post date, languages, topics) in `previews/`, named
by the SHA-256 of its URL: `previews/<first 2 hex digits>/<sha256 hex>.json`. `SPEC.md` on the
branch describes every file.

## Subscribe to a whole topic

1. Click **Download OPML** on the topic's page, or use its direct link:
   `https://raw.githubusercontent.com/feedrule/indexer/output/topic/<topic>/feeds.opml`
2. In your feed reader, look for **Import OPML** (Feedly, Inoreader, NetNewsWire, FreshRSS,
   Miniflux, Thunderbird and most others have it).

A popular topic can hold hundreds of feeds, so you may prefer to browse its page and subscribe
to the ones you like.

For scripts, `links.txt` has the same kind of direct link:
`https://raw.githubusercontent.com/feedrule/indexer/output/topic/<topic>/links.txt`.
These URLs stay the same; the content behind them is replaced whenever the lists are
published. The `output` branch keeps no history, so link to the branch, not to a
commit.

## Which feeds are listed

A feed is listed when:

- it was fetched and read successfully at least once in the last 6 months, and
- its newest post is less than a year old.

Each English feed gets up to 5 topics, chosen by comparing the titles and openings of its English
posts, and its own title and description, with about 6,000 topic names, using the MiniLM sentence
model (all-MiniLM-L6-v2, Apache-2.0). The feeds' own tags, when they are frequent and fit the
posts, add to those topics. An English feed (by the detected languages) for which no topic stands
out takes the first tag of its newest tagged post, if it is one of the topic names (the author's own choice, so less reliable);
otherwise it gets no topic. The tags themselves are not stored or published.

The languages a feed's posts are written in are detected on each post (`en`, `de`, `ar`) and
listed in its preview file; a language counts if it is used in at least 2 posts and a fifth of
them. Only English posts are used for topics. Language detection uses fastText's language
identification model (lid.176, by Facebook AI Research, licensed CC BY-SA 3.0).

Titles and descriptions come from the feeds themselves and are not edited. Being listed means a
feed is alive, not that anyone vouches for its content.

## Where the feeds come from

The sources and their licenses are listed in the
[site's home page](https://feedrule.github.io/indexer/#sources).

The source lists are read as they are today, so feeds added upstream show up here on their own.
YouTube and Reddit feeds are left out: they are platform feeds, not independent sites.

### Feeds from Common Crawl

[Common Crawl](https://commoncrawl.org) is a free archive of billions of web pages. Once a week,
the pipeline takes a crawl from about 5 years ago, collects the feeds those pages linked to, and
checks whether they still work today. A feed that has lasted that long is usually an independent
site worth reading.

The filtering is deliberately simple: a few cheap rules that are easy to understand, rather than
anything clever that would be hard to trust. They are strict, so about **99% of the feeds are
filtered out**. The remaining ~1% are proposed in a pull request, and **a person reviews every one
before it is merged**.

Each run reads only a small sample of the crawl (the start of one random file, about 25 MB of the
tens of terabytes in a crawl), to keep bandwidth and runner time low.

## Add a feed

Open a pull request that adds the feed's URL to [`data/submissions.txt`](data/submissions.txt),
one URL per line. Once it is merged, the feed is loaded right away, checked in one of the next
check runs (usually within two weeks: each run takes a random batch), and listed under its topics if
it passes the rules above.

## License

The code is MIT, see [LICENSE](LICENSE). Each feed URL comes from one of the sources listed in the
[site's home page](https://feedrule.github.io/indexer/#sources), under that source's license.

## How it works

A Python tool, **indexer**, runs as GitHub Actions workflows that share one Postgres
database (Supabase): five on a schedule (`publish` also right after each successful check and when the site's design changes), and one that keeps the
topic data up to date:

| workflow | when | what it does |
| --- | --- | --- |
| `input` | daily, and on every merge that changes the lists in `data/` | reads the sources in `data/sources.toml` and adds new feed URLs to the database |
| `check` | every 3 hours | fetches 500 feeds not checked in the last 14 days and records whether they work, their last post, title, languages and topics |
| `publish` | after each successful check run, when the site's templates or styles change; Mondays as a fallback | writes the topic folders and the site and force-pushes them to the `output` branch |
| `survivors` | Wednesdays | samples an old Common Crawl crawl, checks the feeds it finds, and proposes the active ones in a pull request |
| `cleanup` | Sundays | deletes check results older than 8 months |
| `embeddings` | when the topic list changes | recomputes the topic names' MiniLM embeddings (`data/topics/topics.npy`) and commits them |

Each workflow runs the same Docker images as a local setup: `compose.yaml` defines the app
services (`input`, `check`, `export`, `survivors`, `cleanup`, `embeddings`), a local `postgres`, and a
`liquibase` service that applies the schema in `db/changelog` before every run. In the
workflows, the database settings point at Supabase instead of the local container.

Every feed URL is treated as untrusted. The checker only connects to public IP addresses, follows
at most 3 redirects, caps time and size, and parses XML without loading external entities.

Setup, commands, the data model and the security model are documented in
[DEVELOP.md](DEVELOP.md).
