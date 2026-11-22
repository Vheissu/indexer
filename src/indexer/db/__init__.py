"""Everything about the database: the connection and queries (database.py), the tables as
dataclasses (models.py) and the SQL statements (sql/). Liquibase owns the schema (db/changelog
at the repository root); nothing here creates it."""

from indexer.db.database import DEFAULT_DATABASE_URL, OWN_TABLES, UNDO_TABLES, Database

__all__ = ["DEFAULT_DATABASE_URL", "OWN_TABLES", "UNDO_TABLES", "Database"]
