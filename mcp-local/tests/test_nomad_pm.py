"""Round-trip every nomad-pm MCP tool against the live OpenProject, over real MCP stdio.

Needs the scratch projects first, and they should go afterwards:
    ~/nomad-pm/tests/fixture.sh setup
    mcp-local/.venv/bin/python mcp-local/tests/test_nomad_pm.py
    ~/nomad-pm/tests/fixture.sh teardown

It writes only into pm-test-a and pm-test-b. Two servers run side by side, one as nomad-dev and
one as nomad-manager, so authorship, assignment and my_queue are checked from both sides.
A refusal must come back as an MCP error (isError), never as a quiet success.
"""
import asyncio
import os
import re
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "nomad_pm.py")
TAG = f"rt{int(time.time())}"
EXPECTED = {"projects", "search", "get", "tree", "create", "create_plan", "transition", "update_fields",
            "comment", "watch", "relate", "unrelate", "my_queue", "blocked", "ready", "project_summary"}
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"\n     {detail[:300]}" if detail and not ok else ""), flush=True)


class Agent:
    def __init__(self, session):
        self.s = session

    async def ok(self, tool, **args):
        r = await self.s.call_tool(tool, args)
        text = "\n".join(c.text for c in r.content if hasattr(c, "text"))
        if r.isError:
            raise AssertionError(f"{tool} failed: {text}")
        return text

    async def err(self, tool, **args):
        """The call must fail loudly. Returns the error text."""
        r = await self.s.call_tool(tool, args)
        text = "\n".join(c.text for c in r.content if hasattr(c, "text"))
        return text if r.isError else None


def ids(text):
    return [int(x) for x in re.findall(r"#(\d+) \[", text)]


async def run(dev: Agent, mgr: Agent, tool_names):
    check("exactly the planned tools are exposed", tool_names == EXPECTED, str(sorted(tool_names ^ EXPECTED)))
    check("no delete tool", not any("delete" in t for t in tool_names))

    ps = await dev.ok("projects")
    check("projects lists the scratch projects", "pm-test-a" in ps and "pm-test-b" in ps, ps)

    # ── create_plan: a whole tree with a dependency ──
    plan = [{"key": "f", "type": "Feature", "subject": f"{TAG} feature", "children": [
        {"key": "e1", "type": "Epic", "subject": f"{TAG} epic one", "children": [
            {"key": "t1", "type": "Task", "subject": f"{TAG} task one"},
            {"key": "t2", "type": "Task", "subject": f"{TAG} task two", "blocked_by": ["t1"]},
            {"key": "b1", "type": "Bug", "subject": f"{TAG} bug one"}]},
        {"key": "e2", "type": "Epic", "subject": f"{TAG} epic two", "children": [
            {"key": "s1", "type": "User story", "subject": f"{TAG} story one", "children": [
                {"key": "st1", "type": "Sub-task", "subject": f"{TAG} sub-task one"}]}]}]}]
    out = await dev.ok("create_plan", project="pm-test-a", plan=plan)
    keys = dict(re.findall(r'"(\w+)": (\d+)', out.split("keys:")[-1]))
    k = {a: int(b) for a, b in keys.items()}
    check("create_plan builds 8 work packages and 1 blocks relation",
          "created 8 work packages, 1 blocks relations" in out and len(k) == 8, out)

    before = await dev.ok("search", project="pm-test-a", text=TAG, open_only=False, limit=100)
    e = await dev.err("create_plan", project="pm-test-a", plan=[
        {"type": "Epic", "subject": f"{TAG} bad epic", "children": [{"type": "Sub-task", "subject": f"{TAG} x"}]}])
    after = await dev.ok("search", project="pm-test-a", text=TAG, open_only=False, limit=100)
    check("create_plan with a level skip is refused before anything is created",
          e and "cannot sit under" in e and len(ids(before)) == len(ids(after)), e or "no error")

    # ── create: hierarchy rules ──
    out = await dev.ok("create", project="pm-test-b", type="Task", subject=f"{TAG} cross-project task", parent=str(k["e1"]))
    xt = ids(out)[0]
    check("create: a Task in pm-test-b under an Epic in pm-test-a", "pm-test-b" in out.lower() or "PM test B" in out, out)
    e = await dev.err("create", project="pm-test-a", type="Task", subject=f"{TAG} bad", parent=str(k["f"]))
    check("create: Task under Feature is refused, naming Epic", e and "Allowed parent: Epic" in e, e or "no error")
    e = await dev.err("create", project="pm-test-a", type="Initiative", subject=f"{TAG} bad")
    check("create: Initiative outside nomad-portfolio is refused", e and "nomad-portfolio" in e, e or "no error")
    e = await dev.err("create", project="pm-test-a", type="Sub-task", subject=f"{TAG} bad")
    check("create: orphan Sub-task is refused", e and "needs a parent" in e, e or "no error")

    # ── get and tree ──
    g = await dev.ok("get", id=str(k["t2"]))
    check("get shows the parent, the blocker and the next statuses",
          f"parent: #{k['e1']}" in g and f"blocked #{k['t1']}" in g and "next statuses:" in g and "Ready" in g, g)
    g = await dev.ok("get", id=str(k["e1"]))
    check("get lists children, including the cross-project one",
          all(f"#{k[x]}" in g for x in ("t1", "t2", "b1")) and f"#{xt}" in g, g)
    t = await dev.ok("tree", id=str(k["f"]))
    check("tree from the Feature reaches the Sub-task, three levels down",
          f"      #{k['st1']} [Sub-task]" in t and len(ids(t)) == 9, t)
    t = await dev.ok("tree", project="pm-test-a", depth=2)
    check("tree of a project, depth-limited", f"#{k['f']} [Feature]" in t and "more below" in t, t)

    # ── transition: the workflow is enforced, loudly ──
    e = await dev.err("transition", id=str(k["t2"]), status="Done")
    check("transition New -> Done is refused with the allowed list",
          e and "cannot move New -> Done" in e and "Ready" in e, e or "no error")
    await dev.ok("transition", id=str(k["t2"]), status="Ready")
    r = await dev.ok("ready", project="pm-test-a")
    check("ready leaves out a Ready task with an open blocker", f"#{k['t2']}" not in r, r)
    b = await dev.ok("blocked", project="pm-test-a")
    check("blocked names the blocker", f"#{k['t2']}" in b and f"blocked by #{k['t1']}" in b, b)
    for st in ["Ready", "In Progress"]:
        await dev.ok("transition", id=str(k["t1"]), status=st)
    out = await dev.ok("transition", id=str(k["t1"]), status="Done", comment=f"{TAG} finished by the test")
    check("legal path New > Ready > In Progress > Done, with a comment", "In Progress -> #" in out and "Done" in out, out)
    r = await dev.ok("ready", project="pm-test-a")
    check("ready includes the task once its blocker is Done", f"#{k['t2']}" in r, r)
    b = await dev.ok("blocked", project="pm-test-a")
    check("blocked drops it", f"#{k['t2']}" not in b, b)
    e = await dev.err("transition", id=str(k["f"]), status="Ready")
    check("container workflow: a Feature cannot go to Ready", e and "cannot move" in e, e or "no error")

    # ── update_fields ──
    out = await dev.ok("update_fields", id=str(k["t2"]), subject=f"{TAG} task two renamed", lane="research",
                       assignee="nomad-manager", run_id=f"r-{TAG}", cockpit_link="http://localhost:8099/",
                       due_date="2026-12-31")
    g = await dev.ok("get", id=str(k["t2"]))
    check("update_fields sets subject, lane, assignee, run id, link and due date",
          "renamed" in g and "lane research" in g and "Manager (agent)" in g and f"r-{TAG}" in g
          and "localhost:8099" in g and "due: 2026-12-31" in g, g)
    e = await dev.err("update_fields", id=str(k["t2"]), parent=str(k["f"]))
    check("update_fields refuses a parent that breaks the levels", e and "cannot sit under" in e, e or "no error")
    await dev.ok("update_fields", id=str(k["b1"]), parent=str(k["e2"]))
    g = await dev.ok("get", id=str(k["b1"]))
    check("update_fields moves a Bug to another Epic", f"parent: #{k['e2']}" in g, g)
    e = await dev.err("update_fields", id=str(k["t2"]))
    check("update_fields with nothing to change is refused", e and "nothing to change" in e, e or "no error")

    # ── comment, watch: authorship ──
    await dev.ok("comment", id=str(k["t2"]), text=f"{TAG} a note from dev")
    await mgr.ok("watch", id=str(k["t2"]))
    await dev.ok("watch", id=str(k["t2"]), user="nomad-research")
    g = await mgr.ok("get", id=str(k["t2"]))
    check("the comment is authored by the dev agent", f"Dev (agent): {TAG} a note from dev" in g, g)
    check("watch adds this agent and another by login", "Manager (agent)" in g.split("watchers:")[-1].split("\n")[0]
          and "Research (agent)" in g.split("watchers:")[-1].split("\n")[0], g)

    # ── relate, unrelate ──
    out = await dev.ok("relate", from_id=str(k["t1"]), type="relates", to_id=str(xt))
    check("relate across projects", "relates" in out, out)
    out = await dev.ok("relate", from_id=str(k["s1"]), type="precedes", to_id=str(k["t1"]), lag=2)
    g = await dev.ok("get", id=str(k["s1"]))
    check("precedes with a lag reads back", f"precedes #{k['t1']}" in g and "lag 2" in g, g)
    e = await dev.err("relate", from_id=str(k["t2"]), type="blocks", to_id=str(k["t1"]))
    check("a blocks cycle is refused by OpenProject and surfaces as an error", bool(e), e or "no error")
    out = await dev.ok("unrelate", from_id=str(xt), to_id=str(k["t1"]))
    g = await dev.ok("get", id=str(xt))
    check("unrelate works from the other side", "relates" in out and f"#{k['t1']}" not in g.split("next statuses")[0].split("\n", 1)[-1], g)
    e = await dev.err("unrelate", from_id=str(xt), to_id=str(k["t1"]))
    check("unrelate with nothing to remove is refused", e and "no relation" in e, e or "no error")

    # ── queues and search ──
    q = await mgr.ok("my_queue")
    check("my_queue (manager) lists the task assigned to it", f"#{k['t2']}" in q.split("unassigned")[0], q)
    # The real boards hold hundreds of dev items, and the New bug sorts last, so ask for all of them.
    q = await dev.ok("my_queue", limit=5000)
    check("my_queue (dev) lists unassigned dev-lane work", f"#{k['b1']}" in q and f"#{k['t2']}" not in q,
          q[-2000:])
    order = ["In Progress", "In Review", "Awaiting approval", "Blocked", "Ready", "Backlog", "New"]
    ranks = [order.index(m) for m in re.findall(r"^  #\d+ \[[^]]+\] (.+?) · ", q.split("unassigned")[-1], re.M)]
    check("my_queue (dev) is in queue order across every item", len(ranks) > 1 and ranks == sorted(ranks),
          f"{len(ranks)} items")
    s = await dev.ok("search", text=TAG, type="Epic")
    check("search by text and type", sorted(ids(s)) == sorted([k["e1"], k["e2"]]), s)
    s = await dev.ok("search", text=TAG, lane="research")
    check("search by lane", ids(s) == [k["t2"]], s)
    s = await dev.ok("search", text=TAG, assignee="nomad-manager")
    check("search by assignee login", ids(s) == [k["t2"]], s)
    s = await dev.ok("search", text=TAG, status="Done")
    check("search by status", ids(s) == [k["t1"]], s)
    s = await dev.ok("search", project="pm-test-a", text=TAG)
    check("search open_only leaves out Done", k["t1"] not in ids(s) and len(ids(s)) == 7, s)

    # The scratch projects may hold earlier runs, so compare against the other tools, not constants.
    p = await dev.ok("project_summary", project="pm-test-a")
    n_done = len(ids(await dev.ok("search", project="pm-test-a", status="Done", limit=200)))
    n_epic = len(ids(await dev.ok("search", project="pm-test-a", type="Epic", limit=200)))
    n_ready = len(ids(await dev.ok("ready", project="pm-test-a")))
    check("project_summary counts by status and type, blocked and ready",
          f"Done {n_done}" in p and f"Epic {n_epic}" in p and f"ready {n_ready}" in p and "recent:" in p, p)

    # ── nothing here deletes ──
    s = await dev.ok("search", project="pm-test-a", text=TAG, open_only=False, limit=100)
    check("every work package the test made still exists", len(ids(s)) == 8, s)


async def main():
    env = dict(os.environ)
    sessions = {}
    async with stdio_client(StdioServerParameters(command=sys.executable, args=[SERVER], env={**env, "NOMAD_PM_AS": "nomad-dev"})) as (r1, w1), \
            stdio_client(StdioServerParameters(command=sys.executable, args=[SERVER], env={**env, "NOMAD_PM_AS": "nomad-manager"})) as (r2, w2):
        async with ClientSession(r1, w1) as s1, ClientSession(r2, w2) as s2:
            await s1.initialize()
            await s2.initialize()
            names = {t.name for t in (await s1.list_tools()).tools}
            try:
                await run(Agent(s1), Agent(s2), names)
            except AssertionError as e:
                check("unexpected tool failure", False, str(e))
    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} checks passed · tag {TAG}")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
