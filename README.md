# binaries

Generated binary files, kept off `develop` so its history stays small. Paths mirror the
repository's: a file here at `data/clusters/posts.npy` is used at that path in a checkout of
`develop`:

    git fetch origin binaries
    git checkout origin/binaries -- data/clusters

The branch has no history. Each workflow that writes here replaces only its own folder, keeps the
rest, and force-pushes the result as one new commit.

| path | written by | what |
| --- | --- | --- |
| `data/clusters/posts.npy` | `clusters.yml` | k-means centres of the sentences of recent post titles and descriptions (one unit-length MiniLM row per cluster, float16) |
| `data/clusters/posts.json` | `clusters.yml` | how they were built (model, counts, sources) and, per cluster, its sentences, feeds and rejected feeds |
| `data/clusters/posts.md` | `clusters.yml` | per cluster: top words, counts and example sentences |

The sentences come from untrusted feeds: read them as plain text.
