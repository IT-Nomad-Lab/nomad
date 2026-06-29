#!/usr/bin/env python3
"""Create NOMAD's 7 Mission-Control databases via the Notion API.

Works with the Notion 2025-09 "data-source" API (notion-client 3.x default):
a database owns a child data source, and COLUMNS live on the data source — so
`databases.create(properties=...)` is NOT enough (it only makes the title
column). Per database this script:
  1. creates the database (gets its auto data source + a "Name" title column),
  2. adds the real columns via `data_sources.update(...)`,
  3. renames the title column where the schema wants Title/Action,
  4. adds relations LAST, referencing the target's data_source_id (the new API
     requires data_source_id for relations, not database_id).

Prereqs:
  1. Create an integration at https://www.notion.so/my-integrations
  2. Put its token in .env as NOTION_TOKEN
  3. Create/pick a parent page, connect the integration to it (••• → Connections),
     and put the page id in .env as NOTION_PARENT_PAGE_ID
  4. pip install notion-client python-dotenv
  5. python notion/setup_notion.py
Paste the printed database IDs into .env (NOTION_DB_*). Schema mirrors notion/schema.md.
"""
import os
import sys

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

TOKEN = os.environ.get("NOTION_TOKEN")
PARENT = os.environ.get("NOTION_PARENT_PAGE_ID")
if not TOKEN or not PARENT:
    sys.exit("Set NOTION_TOKEN and NOTION_PARENT_PAGE_ID in .env first.")

notion = Client(auth=TOKEN)  # default (2025-09 data-source) API

AGENTS = ["Orchestrator", "Planner", "Researcher", "Builder", "Writer", "Comms", "Reviewer"]


def select(*names):
    return {"select": {"options": [{"name": n} for n in names]}}


def multi(*names):
    return {"multi_select": {"options": [{"name": n} for n in names]}}


def make_db(title, columns, title_name=None):
    """Create a database, add `columns`, optionally rename the title column.
    Returns (database_id, data_source_id)."""
    res = notion.databases.create(
        parent={"type": "page_id", "page_id": PARENT},
        title=[{"type": "text", "text": {"content": title}}],
    )
    db_id = res["id"]
    ds_id = notion.databases.retrieve(db_id)["data_sources"][0]["id"]
    if columns:
        notion.data_sources.update(ds_id, properties=columns)
    if title_name:  # the auto title column is created as "Name"
        notion.data_sources.update(ds_id, properties={"Name": {"name": title_name}})
    print(f"  {title:<22} {db_id}")
    return db_id, ds_id


def add_relations(ds_id, relations):
    """relations: {prop_name: target_data_source_id}. Non-fatal on failure."""
    props = {name: {"relation": {"data_source_id": tds, "single_property": {}}}
             for name, tds in relations.items()}
    try:
        notion.data_sources.update(ds_id, properties=props)
    except Exception as e:
        print(f"  (relations skipped: {str(e)[:110]})")


def main():
    print("Creating NOMAD mission-control databases...\n")

    projects, projects_ds = make_db("Projects", {
        "Status": select("Active", "Paused", "Done", "Archived"),
        "Owner": {"rich_text": {}},
    })

    goals, goals_ds = make_db("Goals", {
        "Description": {"rich_text": {}},
        "Success Criteria": {"rich_text": {}},
        "Priority": select("Low", "Medium", "High"),
        "Target Date": {"date": {}},
    })
    add_relations(goals_ds, {"Project": projects_ds})

    milestones, milestones_ds = make_db("Milestones", {
        "Due Date": {"date": {}},
        "Status": select("Not Started", "In Progress", "Review", "Done"),
        "% Complete": {"number": {}},
    }, title_name="Title")
    add_relations(milestones_ds, {"Goal": goals_ds})

    tasks, tasks_ds = make_db("Tasks", {
        "Assigned Agent": select(*AGENTS),
        "Status": select("Backlog", "In Progress", "Review", "Done", "Blocked"),
        "Output Link": {"url": {}},
    }, title_name="Title")
    add_relations(tasks_ds, {"Milestone": milestones_ds})

    activity, activity_ds = make_db("Agent Activity Log", {
        "Agent": select(*AGENTS),
        "Timestamp": {"date": {}},
        "Detail": {"rich_text": {}},
    }, title_name="Action")
    add_relations(activity_ds, {"Task": tasks_ds})

    approvals, approvals_ds = make_db("Approvals", {
        "Type": select("Send Email", "Merge to main", "External Invite",
                       "Share File", "Publish", "Delete", "Spend"),
        "Status": select("Pending", "Approved", "Rejected"),
        "Requested By": select(*AGENTS),
        "Context": {"rich_text": {}},
        "Requested At": {"date": {}},
    }, title_name="Action")

    knowledge, knowledge_ds = make_db("Knowledge Base", {
        "Type": select("Brief", "Decision", "Reference", "Snippet"),
        "Tags": multi(),
        "Source": {"url": {}},
    }, title_name="Title")
    add_relations(knowledge_ds, {"Project": projects_ds})

    print("\nDone. Paste these into .env:\n")
    print(f"NOTION_DB_PROJECTS={projects}")
    print(f"NOTION_DB_GOALS={goals}")
    print(f"NOTION_DB_MILESTONES={milestones}")
    print(f"NOTION_DB_TASKS={tasks}")
    print(f"NOTION_DB_ACTIVITY={activity}")
    print(f"NOTION_DB_APPROVALS={approvals}")
    print(f"NOTION_DB_KNOWLEDGE={knowledge}")


if __name__ == "__main__":
    main()
