"""S6 · add `provider_id` to the outbox table.

The v2-send webhook answers from its last node now, so its reply carries Gmail's own message id.
That id is the only thing in the outbox that can be checked against the mailbox afterwards: `sent`
is this system's claim, and the id is the receipt behind it. Without somewhere to put it the reply
would be read once and thrown away.

Idempotent, and it reuses `migrate_outbox_status.migrate` rather than growing a second copy of the
add-a-column dance. Nothing is backfilled: every row written before this had no id to record, and
an empty one means "not recorded", which is true.

    python3 v2/migrate_outbox_provider_id.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from migrate_outbox_status import migrate  # noqa: E402

NEW_COLUMNS = [
    {"column_name": "provider_id", "title": "provider_id", "uidt": "SingleLineText"},
]


if __name__ == "__main__":
    migrate(columns=NEW_COLUMNS)
    print("outbox provider_id migration done")
