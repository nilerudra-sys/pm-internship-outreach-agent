"""python -m database  ->  create or upgrade the SQLite database."""

from database.db import init_db

print(f"Initialised database at {init_db()}")
