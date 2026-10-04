"""Copy every SQLite table into MongoDB (database: stocks).

Safe to re-run: each collection is dropped and rebuilt.
SQLite is opened read-only, so this can never change the real data.
"""
import os
import sqlite3

from pymongo import MongoClient

DATA_DIR = os.environ.get("STOCKAPP_DATA_DIR", "/var/lib/stockapp")
DB_PATH = os.path.join(DATA_DIR, "stock_reports.db")

src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
src.row_factory = sqlite3.Row
dst = MongoClient("mongodb://127.0.0.1:27017")["stocks"]

tables = [r[0] for r in src.execute(
    "SELECT name FROM sqlite_master "
    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]

for t in tables:
    rows = [dict(r) for r in src.execute(f'SELECT * FROM "{t}"')]
    for r in rows:
        if "id" in r:
            r["_id"] = r.pop("id")
    dst[t].drop()
    if rows:
        dst[t].insert_many(rows)
    print(f"  {t}: {dst[t].count_documents({})}")

src.close()
