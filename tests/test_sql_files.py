"""Every statement db.py loads from sql/ exists, and every file there is used (no database)."""

import re
from importlib.resources import files

import indexer.db


def test_every_sql_file_is_used_and_present() -> None:
    source = files("indexer").joinpath("db.py").read_text()
    used = set(re.findall(r'_query\("(\w+)"\)', source))
    present = {p.name.removesuffix(".sql") for p in files("indexer").joinpath("sql").iterdir()}
    present.discard("__pycache__")
    assert used == present
    for name in used:
        assert indexer.db._query(name).strip()
