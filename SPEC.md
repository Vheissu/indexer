# Output files

| Path | Contents |
| --- | --- |
| `tag/tags.txt` | Published tag names, one per line, most feeds first. |
| `tag/<tag>/links.txt` | Feed URLs, one per line, newest post first (at most 50,000). |
| `tag/<tag>/feeds.opml` | OPML 2.0, the 500 most recently active feeds, by URL. |
| `all/links.txt` | Every listed feed, tagged or not, newest post first. |
| `all/feeds.opml` | OPML 2.0 of every listed feed, sorted by URL. |
| `previews/<h:2>/<h>.json` | One JSON file per listed feed (see below). |
| `docs/index.html` | Website (GitHub Pages serves `docs/`): the 50 largest tags, a search over all published tags, and the sources. |
| `docs/tag/<tag>/index.html` | Page of the first 200 of those feeds: a feedrule.com link (email digests, form prefilled with the feed), title and "Website" (links to the website), domain, "Feed" (links to the feed), up to 10 of the feed's other tags (most widespread first), last post date. |
| `docs/style.css`, `docs/feedrule.svg` | The site's stylesheet and feedrule.com's icon. |
| `docs/sitemap.xml` | The index and every tag page. |
| `docs/.nojekyll` | Tells GitHub Pages to serve the files as they are. |

`<h>` is the SHA-256 (hex) of the feed URL in UTF-8; `<h:2>` is its first 2 characters.

Preview fields: `url` (the feed URL), `title`, `bytes` (feed size at the last successful check),
`last` (date of the newest post, `YYYY-MM-DD`), `tags` (all the feed's tags, sorted; only those in
`tag/tags.txt` have a folder). `title`, `bytes` and `last` may be `null`.

Tags are lower-case `a-z`, `0-9`, `-` and `_`. Titles come from the feeds: treat them as untrusted
plain text, never as HTML.
