# Development

How indexer works, how to run it, and how it is deployed. What the lists are, which feeds get
listed, where they come from, and when each workflow runs are in the [README](README.md); this
file doesn't repeat them.

## Commands

| stage | command | compose service | workflow |
| --- | --- | --- | --- |
| load feed URLs from the sources | `indexer load-inputs data/sources.toml` | `input` | `input.yml` |
| propose old Common Crawl feeds still active | `indexer survivors` | `survivors` | `survivors.yml` |
| check a random batch of inputs | `indexer run --from-inputs` | `check` | `check.yml` |
| write the published lists | `indexer export` | `export` | `publish.yml` |
| delete old checks | `indexer cleanup` | `cleanup` | `cleanup.yml` |
| **delete every table and all data** | `indexer drop-tables --yes` | `drop-tables` | `drop-tables.yml` (manual) |

All state is in Postgres: locally the compose `postgres` service, in deployment Supabase. The
schema is managed by Liquibase (`db/changelog`).

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.13) and Docker with
Compose v2.

```sh
cp .env.example .env                     # then set POSTGRES_PASSWORD in it (git-ignored)
export PGPASSWORD="$(sed -n 's/^POSTGRES_PASSWORD=//p' .env)"   # for uv run
docker compose up -d postgres            # Postgres on 127.0.0.1:5432 (user and db: indexer)
docker compose run --rm liquibase        # apply the schema
uv sync

uv run indexer run data/urls.txt -v    # check 100 sample feeds; -v lists each failure and why
uv run indexer load-inputs data/sources.toml
uv run indexer run --from-inputs --batch 50
uv run indexer export                  # writes ./output
```

Every command also runs in a container (`docker compose run --rm <service> [args]`), which is
how the workflows run them; rebuild after code changes with `docker compose build`. The
containers run as a non-root user with a read-only filesystem, no Linux capabilities and
`no-new-privileges`, and the address rules apply inside them too: `http://postgres/` resolves to
the database container's private address and is refused like any other non-public host.

Look at the results of the latest run:

```sh
docker compose exec postgres psql -U indexer -c \
  "select coalesce(error, 'ok') as outcome, count(*) from health_checks
   where run_id = (select max(id) from runs) group by 1 order by 2 desc"
```

Stop with `docker compose down` (add `-v` to delete the data volume). Point any command at
another database with `DATABASE_URL` or `--database-url`, a URL or a libpq keyword string
(`host=... dbname=...`); the password may come from `PGPASSWORD`.

## Inputs

The `inputs` table holds every feed URL to check, one row per URL. It only grows: nothing ever
deletes from it, so a broken or truncated upstream list adds nothing harmful and removes
nothing.

| column | meaning |
| --- | --- |
| `url` | the feed URL, in the form the checker validates (primary key) |
| `source` | where it was found: the list file's URL, or the Common Crawl file |
| `source_license` | that list's license (an SPDX id) |
| `first_seen_at` | when the URL was first loaded |
| `last_checked_at` | when it was last checked; `null` means never |

### Curated lists: data/sources.toml

`data/sources.toml` names the lists, each with its license and the URL(s) of its list file(s):

```toml
[[source]]
name = "kagi-smallweb"
license = "MIT"
format = "lines"                    # or "opml"
homepage = "https://github.com/kagisearch/smallweb"
urls = ["https://raw.githubusercontent.com/kagisearch/smallweb/main/smallweb.txt"]
```

The URLs point at a branch, not a commit, so each load reads the lists as they are today and
new feeds arrive without anyone editing the config. A source can also use `files`, paths in this
repository relative to `data/sources.toml`, read from disk. Files are downloaded with the
guarded fetcher; OPML is parsed with defusedxml, falling back to reading the `xmlUrl` attributes
when a hand-edited file is not well-formed (common). URLs are validated with the checker's
scheme, credential and port rules.

- **Licenses.** A URL found in several lists is credited to the most permissive one, by the
  `[licenses] ranking` (on a tie, the source listed first). Re-loading only moves a URL to a
  strictly more permissive source. Only openly licensed lists are used.
- **Exclusions.** Feeds on the registrable domains in `[exclude] domains` (and their subdomains)
  are never loaded.
- **Failures.** A source that fails (file gone, malformed) is skipped and reported, and the
  others still load. In GitHub Actions a skipped source is a warning annotation on the run; the
  command fails only if no source could be read.

The sources and their licenses are listed on the [site's home page](https://feedrule.github.io/indexer/#sources) (`site.SOURCES`).

### Submissions

`data/submissions.txt` is an ordinary source (`files = ["submissions.txt"]`), listed last in
`data/sources.toml` so a feed that is also in another list keeps that list as its source.
`uv run pytest` checks that every line of it (and of `data/survivors.txt`) is a valid, unique URL.

### Survivors from an old crawl

`indexer survivors` looks for feeds that have lasted, and **proposes** them for review rather
than adding them. It picks the crawl that ended closest to `--years-ago` (5) years ago and
downloads the first `--slice-mb` (25) MB of one random WAT file with a range request,
dropping the record cut off at the end. [Common Crawl](https://commoncrawl.org)'s WAT files record
the `<link>` elements of every crawled page; survivors collects every feed a page advertises with
`<link rel="alternate">`, validates it like any input, applies `[exclude] domains`, removes session parameters (`sid`, `PHPSESSID`, `jsessionid`, ...), folds platform
format variants into one URL (WordPress `/feed/atom/`, `/feed/rdf/`, `/feed/rss/`, `/feed/rss2/`
become `/feed/`; Blogger `?alt=rss` becomes the default feed; Joomla `type=atom` becomes
`type=rss`), skips WordPress category, tag and author feeds (`/category/news/feed/`, a subset of
the main feed) without fetching them, and checks the rest today with the guarded fetcher (no
retries). A feed is proposed only if:

- it parses, and its newest item **with a category or tag** is from the last `--active-days` (243, about 8 months)
  days (a date more than a day in the future doesn't count; platform defaults `uncategorized`,
  `uncategorised` and `general` are not tags);
- it is **labelled English**: its language is `en` or `en-*`. This is the publisher's label, not
  detection: WordPress says `en-US` unless the owner changes it, so a few non-English sites pass;
- it **has a title**;
- its title and tags (all of them) contain none of the `[commoncrawl] exclude_keywords` in `data/sources.toml`
  as a whole word.
  This list is for Common Crawl only: the curated lists and submissions are not filtered by it
  (tried on their 9,048 feeds, it would flag 14: 3 adult, the rest blogs like "Budgets Are Sexy");
- it is **served over https today**: the URL after redirects is `https://`. An old `http://` URL
  that now redirects to https is proposed as its https target; one still served over plain http
  is skipped (no `https://` is tried);
- it is not already in `data/survivors.txt` or the inputs table.

Feeds already there are skipped before checking (and again after, for the URL a redirect led
to). The rest are checked in random order, 200 at a time, and the run stops once `--max-feeds`
(50) pass, so a run proposes at most 50 and stops downloading early.

The finds are appended to `data/survivors.txt`, each as a comment line (title, language, date of
the newest tagged item, tags), the WAT file it came from, and the URL. Each workflow run commits
that to a **new branch**, `survivors-<UTC timestamp>` (for example `survivors-20261119-041105`),
and opens **its own pull request** into `develop`, with one
section per feed: its title, URL, date of the newest tagged item, tags, and the links of its 3
latest posts as written, all as code spans so nothing is clickable and spam domains are easy to
spot. The report stays under GitHub's 65,536-character limit: feeds that don't fit are only
counted ("... and N more feeds: see the diff"). Review the diff, delete the URL line of any feed you don't want, and merge (or close the pull
request to drop the whole batch): the
input job then loads the file on the push to `develop` (source `survivors`, license
`LicenseRef-CommonCrawl-ToU`, ranked last). Every run starts from `develop`, so a pull request never carries an older run's proposals.
Two open pull requests both append to the end of the file, so merging one makes the other
conflict: merge or close them in order (GitHub's "Update branch" or a rebase resolves it; keep both
blocks). URLs in another open pull request are not skipped, so the same feed can be proposed twice. A deleted URL is not remembered after merging,
so in the unlikely case a later random slice finds it again, it is proposed again.

Started by hand (Actions → survivors → Run workflow), the workflow takes `slice-mb`, `years-ago`
`active-days` and `max-feeds` as inputs; the weekly run uses the defaults (25, 5, 243, 50).

The workflow needs the repository setting **Settings → Actions → General → "Allow GitHub Actions
to create and approve pull requests"**. Titles and tags are untrusted: in the file they are
flattened to one line, and in the pull request they are shown as code, so they can't add URLs,
mention people or link issues.

```sh
mkdir -p output && HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose run --rm survivors \
  survivors --slice-mb 5 --report output/survivors.md
```

In the pilot, a 20 MB slice of the 2021-49 crawl held about 3,200 pages and 2,300 feed URLs from
900 sites. 216 had posted within a year; 56 (38 sites) also had tags and were served over https,
and 14 of those (8 sites) were labelled English. With the default-tag, category-feed, session and
format rules that becomes 10 (6 sites). The first real run (5 slices of 20 MB, CC-MAIN-2021-43)
checked 11,944 feed URLs: 4,685 worked, 612 had a recent tagged item, 209 were labelled English
and 104 were on https (0.87%). A run reads one 25 MB slice: about 3,000 feed URLs to check. One file
gives as many distinct feeds per MB as several smaller slices (about 125 per MB in samples of 5 to
30 MB from one file and of 5 × 20 MB), so there is no reason to spread the sample. The checks made
here are not stored: once merged, the inputs get their regular checks from `check`. If the slice
fails to download (Common Crawl answers busy periods with 503), the run fails without retrying;
the next weekly run picks another file.

## Checking

```sh
uv run indexer run FILE                     # every URL in a text file
uv run indexer run --from-inputs            # a batch of 500 inputs not checked in 14 days
uv run indexer run --from-inputs --batch 50 --stale-days 7 -v
```

A URL file has one URL per line; blank lines and `#` comments are ignored, and duplicates are
checked once. `data/urls.txt` is a 100-feed sample of
[simevidas/web-dev-feeds](https://github.com/simevidas/web-dev-feeds) (MIT) for quick runs.

`--from-inputs` checks a uniformly random sample of the inputs never checked, or last checked
more than `--stale-days` ago. Storing a result sets `last_checked_at` whatever the outcome, so
repeated runs work through every URL before revisiting any: at 8 runs a day of 500, a full pass
over ~47,000 curated inputs takes about 12 days.

Each result is written as soon as its check finishes, so an interrupted run keeps what it has
done (its `finished_at` stays empty). `-v` prints each failed URL with the reason; successes only
appear in the summary. Other options: `--concurrency` (20; check.yml passes 50), `--per-host` (2), `--deadline`
(7 s). The `ETag` and `Last-Modified` of each successful response are stored as sent (values
that are not printable ASCII, or longer than 1024 characters, are dropped); nothing sends them
back yet.

## Published lists

`indexer export` writes the data files: `output/tag/<tag>/` (`feeds.opml`, `links.txt`),
`output/tag/tags.txt` (tag names, most feeds first), `output/all/` (`links.txt`, `feeds.opml`:
every listed feed) and `output/previews/` (one JSON file per listed feed, for a client-side search
page: fetch a tag's `links.txt`, then the previews of the results shown; see
`export.preview_path` and `export.render_preview`). `links.txt` files are most recently active
first. It also writes the website, `output/docs/` (`src/indexer/site.py`): `index.html` (the 50 largest
topics, a search box over all of them, and the sources), `tag/<tag>/index.html` (the 200 most recently active feeds, each with a
feedrule.com link prefilled with the feed), `style.css` and `feedrule.svg` (package data:
`src/indexer/style.css`, `src/indexer/feedrule.svg`), `sitemap.xml` and `.nojekyll`. GitHub Pages
serves the `output` branch's `/docs` folder; the pages link the data files on
raw.githubusercontent.com, so they don't count toward the Pages size limit. `--site-url` (default
`https://feedrule.github.io/indexer/`) is used for canonical links and the sitemap; `--data-url` for the data links. There is
no Markdown in the output except `SPEC.md`.

The files and listing rules are described in the [README](README.md#which-feeds-are-listed). The
limits are options: `--top-tags`, `--success-days`, `--item-years`, `--ignore-tag`. Titles and
URLs come from the feeds: the pages are Jinja templates (`src/indexer/templates/`) with
autoescaping on, the OPML is escaped by hand, and only http(s) URLs become
links (a site URL could be `javascript:`); feed and site links are `rel="nofollow"`. `SPEC.md`,
the hand-written description of these files at the repository root, is copied into the output
(`--spec`; the compose `export` service mounts it read-only). The output directory is cleared on
every run, but only if it is empty or holds the `.indexer-output` marker, so a wrong path cannot
wipe anything else.

```sh
mkdir -p output && HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose run --rm export
```

`publish.yml` force-pushes `output/` as a single fresh commit to the `output` branch, which never
accumulates history. It is triggered by `workflow_run` on `check`, so it uses `publish.yml` as it
is on the default branch. It only reads the database.

## Cleanup

`indexer cleanup` deletes health checks older than 8 months, and runs older than that with no
checks left. `feeds` (each feed's latest metadata) and `inputs` are kept. At about 4,000 checks a
day this holds `health_checks` near a million rows (~320 MB with indexes). It refuses fewer than
7 months, since the published lists look back 6.

## Starting over: drop-tables

`indexer drop-tables --yes` drops this project's tables (`inputs`, `feeds`, `health_checks`,
`runs`) and Liquibase's `databasechangelog` and `databasechangeloglock`, with all their data. It
drops only those names, in the connection's schema, so other tables in the database are left
alone; without `--yes` it refuses. The next migration (every workflow runs one first) recreates
the schema empty, and the input job reloads the URLs from `data/sources.toml`. The check history,
and any inputs that exist only in the database, are lost for good.

`drop-tables.yml` runs it against Supabase. It never runs on a schedule: start it from the
Actions tab and type `delete all tables` as the confirmation; anything else skips the job. It
waits for any running pipeline job (same concurrency group).

## Deployment: GitHub Actions and Supabase

The workflows in `.github/workflows` run everything through docker compose, exactly as
locally: build the images, migrate the schema with the `liquibase` service, then run the stage's
service, all with `docker compose run --no-deps` so the local `postgres` service never starts.
The database settings in `compose.yaml` default to the local database and are overridden from
the environment (`LIQUIBASE_COMMAND_URL`, `LIQUIBASE_COMMAND_USERNAME`,
`LIQUIBASE_COMMAND_PASSWORD`, `DATABASE_URL`, `PGPASSWORD`). The workflows share one concurrency
group, so they never overlap; they use a read-only repository token (except `publish.yml`, which pushes `output`, and
`survivors.yml`, which pushes `survivors` and opens its pull request) and
pin the checkout action by commit SHA. They need these repository secrets:

| secret | value |
| --- | --- |
| `SUPABASE_DB_HOST` | the **session pooler** host, e.g. `aws-0-<region>.pooler.supabase.com`: GitHub-hosted runners have no IPv6, and Supabase's direct database host is IPv6-only |
| `SUPABASE_DB_PORT` | `5432` (session mode, not the 6543 transaction pooler) |
| `SUPABASE_DB_NAME` | `postgres` |
| `SUPABASE_DB_USER` | `postgres.<project-ref>` |
| `SUPABASE_DB_PASSWORD` | the database password |

To run the same steps from your machine against Supabase, put those values in a git-ignored
`supabase.env` and use `docker compose --env-file supabase.env run --rm --no-deps <service>`.

## Data model

`runs` has one row per `indexer run` (`id` is the run number, `started_at`, `finished_at`).

`health_checks` has one row per URL per run, with only per-check data (about 330 bytes per row):

| column | meaning |
| --- | --- |
| `url` | the URL as given |
| `final_url` | the last URL requested (after redirects) |
| `http_status` | status of the last response |
| `parent_domain` | registrable domain of `url`, from the public suffix list bundled with `publicsuffixlist` |
| `redirected` | at least one redirect was followed |
| `first_redirect_status` | status of the first redirect: 301/308 permanent (update the URL), 302/303/307 temporary |
| `ip_address` | the final hop's server address; for a failed connection, the last address tried |
| `file_size` | bytes of the final body, after decompression |
| `item_count` | number of `<item>` / `<entry>` elements |
| `last_item_date` | newest published/updated date among the items |
| `ttl` | RSS `<ttl>` in minutes, if present and valid |
| `fetch_time_ms` | request start until the body is fully read, redirects included |
| `response_time_ms` | time to the response headers of the final request |
| `error` | `null` on success, otherwise one fixed identifier (below) |
| `etag`, `last_modified` | the validators of the final 200 response, as sent |

`feeds` has one row per feed URL with its **latest** metadata, updated in place: a check that
parses the feed replaces every column (a field the feed no longer has becomes `null`), a failed
check keeps the last known values.

| column | meaning |
| --- | --- |
| `url` | the feed URL (primary key) |
| `updated_at`, `parsed_at` | when a check last changed this row; when one last parsed the feed |
| `feed_kind` | `rss`, `rdf`, `atom` or `atom03` |
| `generator` | the feed's `<generator>` (or RDF `admin:generatorAgent`) |
| `site_url` | the website the feed names (RSS channel `<link>`, Atom alternate link) |
| `self_url` | where the feed says it lives (`atom:link rel="self"`) |
| `new_feed_url` | `<itunes:new-feed-url>`: the feed declares it has moved |
| `title` | feed title (Atom `<title>`, RSS channel `<title>`) |
| `subtitle` | Atom `<subtitle>` or RSS `<description>`, up to 500 characters |
| `categories` | feed-level categories (RSS `<category>`, `dc:subject`, Atom `<category term>`) |
| `item_categories` | categories of all items, from the same elements |
| `itunes_category` | `<itunes:category>` paths such as `Technology > Software` |
| `language` | lower-case language tag (`<language>`, `dc:language`, `xml:lang`) |
| `media_type` | `video` or `podcast` when at least half the items carry video or audio, otherwise `text`; `null` for an empty feed |
| `icon`, `logo` | Atom `<icon>`; Atom `<logo>`, RSS `<image><url>` or `<itunes:image>` |
| `rights`, `license` | Atom `<rights>`, RSS `<copyright>` or `dc:rights`; a license URL (`link rel="license"`, `creativeCommons:license`) |

Both category columns are normalized: each value is split on commas and lower-cased; `+` becomes
`plus` and `#` becomes `sharp`; accents are folded to ASCII; only `a-z`, `0-9`, `-`, `_` and single
spaces are kept (`C++` → `cplusplus`, `Node.js` → `nodejs`); duplicates are removed, and the rest
are sorted and joined with `, `, up to 2048 characters of whole terms. Other text columns have
markup stripped and whitespace collapsed, and are capped at 200 characters unless noted. URL
columns come from untrusted content: resolved against `final_url`, kept only if they are absolute
`http(s)` URLs without control characters (up to 2048 characters), and never fetched.

Error identifiers (`src/indexer/errors.py`): `invalid_url`, `blocked_ip`, `dns_nxdomain` (the
name does not exist or has no addresses), `dns_temporary`, `dns_error`, `connect_error`,
`tls_error`, `timeout`, `too_many_redirects`, `bad_redirect`, `rate_limited` (429, or 503 with
`Retry-After`), `http_4xx`, `http_5xx`, `http_other`, `header_too_large`, `protocol_error`,
`too_large`, `unsupported_encoding`, `decode_error`, `not_a_feed`, `parse_error`,
`internal_error`.

## Security model

Every URL is untrusted. indexer:

- accepts only `http`/`https`, rejects credentials in the URL, and allows ports 80 and 443 only;
- connects only to public IP addresses. Private, loopback, link-local (including
  169.254.169.254), Azure's platform address 168.63.129.16 (reachable from GitHub-hosted
  runners), CGNAT, multicast, reserved, documentation, ULA and similar ranges are blocked,
  including their IPv4-mapped, NAT64 and 6to4 forms (`src/indexer/net/ippolicy.py`);
- is safe against DNS rebinding: the address check runs inside aiohttp's connector, on the exact
  addresses the socket then connects to (`GuardedResolver`, DNS cache off), and again in the
  socket factory right before each socket is created. There is no second lookup;
- follows at most 3 redirects by hand, re-applying every rule to each hop;
- enforces a 7 s deadline per fetch (redirects and body included), a 3 s connect timeout, a
  5 MB body limit counted after decompression (gzip bombs stop at the limit), header size/count
  limits, GET only, no cookies, and ignores proxy environment variables;
- parses XML with defusedxml (no entity expansion, no external references, DTDs never loaded)
  and ignores `Content-Type`;
- runs 20 fetches at once by default (50 in check.yml), at most 2 per host; a failure never stops the run.

Lists and Common Crawl files are downloaded through the same fetcher (with larger size limits).
Text from feeds is escaped wherever it is published. The User-Agent is
`indexer/<version> (awesome-rss feed health checker)`.

## Development

Source layout (`src/indexer/`):

| path | contents |
| --- | --- |
| `cli.py` | the `indexer` commands |
| `db.py` | all database reads and writes |
| `errors.py` | the error identifiers |
| `export.py` | the published lists (data files) |
| `site.py` | the website in `output/docs/`: prepares the data for the Jinja templates in `templates/` |
| `sources.py` | data/sources.toml and loading the lists into inputs |
| `net/` | fetching untrusted URLs: `fetch.py` (the guarded fetcher), `validate.py` (URL rules, URL files), `resolver.py`, `ippolicy.py`, `urls.py`, `domains.py` |
| `check/` | `runner.py` (one check per URL), `feed.py` (parsing RSS/Atom) |
| `survivors/` | `commoncrawl.py` (WAT sampling and filters), `proposals.py` (survivors.txt and the PR report) |

```sh
uv run pytest            # no network needed: local test server and fake DNS
uv run ruff format . && uv run ruff check .
uv run mypy --strict src
```

The database tests run when `INDEXER_TEST_DATABASE_URL` is set, e.g.
`INDEXER_TEST_DATABASE_URL=postgresql://indexer@127.0.0.1:5432/indexer`, with the password in
`PGPASSWORD`.

The schema lives in `db/changelog/changes/NNN-*.yaml`, each listed in
`db/changelog/db.changelog-master.yaml`. Never edit an existing changeset: add a new numbered file,
list it in the master changelog, and run `docker compose run --rm liquibase`. Liquibase 5 ships no
JDBC drivers, so `db/liquibase.Dockerfile` adds the PostgreSQL driver at a pinned version,
verified by its SHA-256 checksum.
