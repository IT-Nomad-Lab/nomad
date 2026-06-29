# notion — optional Notion mission control

NOMAD's **v1 / rollback** data layer. Mission control now runs on **NocoDB-on-Postgres** (see
[`v2/`](../v2/)), but Notion is still supported as a human-readable mirror and the rollback
target — selected at runtime by `NOMAD_SOURCE=notion`.

This directory creates and documents the **7 Mission-Control databases**:
Projects · Goals · Milestones · Tasks · Activity · Approvals · Knowledge.

| File | Role |
|---|---|
| `schema.md` | The 7 databases and their columns/relations. |
| `setup_notion.py` | Creates them via the Notion API. |

## Setup

```bash
pip install notion-client python-dotenv
python setup_notion.py     # prints the database IDs → paste into .env as NOTION_DB_*
```

Prereqs:
1. Create an integration at <https://www.notion.so/my-integrations> and put its token in `.env`
   as `NOTION_TOKEN`.
2. Create/pick a parent page, connect the integration to it (••• → Connections), and set
   `NOTION_PARENT_PAGE_ID` in `.env`.

> **Notion 2025-09 "data-source" API note:** with `notion-client` 3.x, `databases.create(
> properties=…)` only makes the title column — columns live on the database's child *data
> source*. `setup_notion.py` handles this correctly (creates the DB, adds columns via
> `data_sources.update`, renames title columns, then adds relations last using
> `data_source_id`). If you adapt it, keep that ordering.
