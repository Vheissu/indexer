"""Every statement database.py loads from sql/ exists, and every file there is used."""

import re
from importlib.resources import files

import indexer.db.database


def test_every_sql_file_is_used_and_present() -> None:
    source = files("indexer.db").joinpath("database.py").read_text()
    used = set(re.findall(r'_query\("(\w+)"\)', source))
    present = {p.name.removesuffix(".sql") for p in files("indexer.db").joinpath("sql").iterdir()}
    present.discard("__pycache__")
    assert used == present
    for name in used:
        assert indexer.db.database._query(name).strip()
