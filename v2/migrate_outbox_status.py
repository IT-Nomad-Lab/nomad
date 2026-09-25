"""S4 · add `status` and `error` to the outbox table.

`setup_nocodb.py` skips a table that already exists, so a new column in its TABLES dict never
reaches a live base. This is the migration that does, and it is idempotent: run it as often as you
like.

Auth goes through `nocodb.NocoDB`, which prefers the long-lived `NC_API_TOKEN` over a signin. A
second auth path here would be a second thing to rotate, and it would open a user session that
invalidates the operator's own NocoDB login.

Existing rows are left alone rather than backfilled to `accepted`. Every one of them was written by
the code that stamped `delivered_at` whether or not the send worked, so we do not know which ones
arrived. An empty status means "written before this was recorded", which is true. Filling it in
would be inventing the very fact the column exists to stop us inventing.

    python3 v2/migrate_outbox_status.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nocodb import NocoDB  # noqa: E402

NEW_COLUMNS = [
    {"column_name": "status", "title": "status", "uidt": "SingleLineText"},
    {"column_name": "error", "title": "error", "uidt": "LongText"},
]


def migrate(db=None, table="outbox", columns=NEW_COLUMNS):
    """Add any missing column to `table`. Returns the names it actually added."""
    db = db or NocoDB()
    db._auth()
    tid = db.table_id(table)
    have = {c["title"] for c in db._req("GET", f"/api/v2/meta/tables/{tid}")["columns"]}
    added = []
    for col in columns:
        if col["title"] in have:
            print(f"  · {col['title']} exists")
            continue
        db._req("POST", f"/api/v2/meta/tables/{tid}/columns", col)
        added.append(col["title"])
        print(f"  + added {col['title']}")
    return added


if __name__ == "__main__":
    migrate()
    print("outbox migration done")
