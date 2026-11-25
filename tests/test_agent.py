"""Tests run fully offline: the CRM is in-memory and Claude is replaced by a scripted fake client."""

from types import SimpleNamespace as NS

import pytest

from sales_agent import crm
from sales_agent.agent import SalesAgent
from sales_agent.scoring import comment_signal
from sales_agent.tools import MAX_OPEN_LEADS_PER_REP, TOOLS, ToolError, run_tool


@pytest.fixture
def conn():
    c = crm.connect(":memory:")
    crm.seed(c)
    return c


def open_lead_ids(conn, rep_name=None, n=3):
    sql = ("SELECT l.id FROM leads l JOIN reps r ON r.id=l.rep_id WHERE l.status IN "
           "('new','contacted','interested','negotiation')")
    params = ()
    if rep_name:
        sql += " AND r.name=?"
        params = (rep_name,)
    return [r[0] for r in conn.execute(sql + " LIMIT ?", (*params, n))]


# ---------------- tools & rules ----------------

def test_every_tool_schema_is_strict():
    for t in TOOLS.values():
        s = t.spec()
        assert s["strict"] is True
        assert s["input_schema"]["additionalProperties"] is False
        assert set(s["input_schema"]["required"]) == set(t.properties)


def test_arabic_and_english_comments():
    assert comment_signal(["العميل مهتم جداً ويريد عرض سعر"]) > 0
    assert comment_signal(["غير مهتم حالياً"]) < 0
    assert comment_signal(["Not interested, price too high"]) < 0  # not counted as "interested"


def test_score_leads_saves_scores(conn):
    out = run_tool(conn, "score_leads", {"lead_ids": None})
    assert '"scored": 60' in out
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE score IS NULL").fetchone()[0] == 0


def test_cannot_reassign_closed_lead(conn):
    closed = conn.execute("SELECT id FROM leads WHERE status='won' LIMIT 1").fetchone()[0]
    with pytest.raises(ToolError, match="Closed leads"):
        run_tool(conn, "reassign_leads", {"lead_ids": [closed], "to_rep_name": "Bilal", "reason": "x"})


def test_rep_capacity_limit(conn):
    ids = open_lead_ids(conn, n=MAX_OPEN_LEADS_PER_REP)
    with pytest.raises(ToolError):
        run_tool(conn, "reassign_leads", {"lead_ids": ids, "to_rep_name": "Bilal", "reason": "x"})


def test_invalid_status_transition(conn):
    lead = conn.execute("SELECT id FROM leads WHERE status='new' LIMIT 1").fetchone()[0]
    with pytest.raises(ToolError, match="not allowed"):
        run_tool(conn, "update_lead_status", {"lead_id": lead, "new_status": "won", "note": "x"})


# ---------------- agent loop with a fake Claude ----------------

def tool_use(name, args, id_="tu_1"):
    return NS(type="tool_use", name=name, input=args, id=id_)


def reply(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop)


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})  # snapshot: the agent keeps appending
        return self.script.pop(0)


def statuses(conn):
    return [r[0] for r in conn.execute("SELECT status FROM audit_log ORDER BY id")]


def test_sensitive_call_rejected_by_manager(conn):
    ids = open_lead_ids(conn, "Ali", 2)
    fake = FakeClient([
        reply(tool_use("reassign_leads", {"lead_ids": ids, "to_rep_name": "Bilal", "reason": "balance"})),
        reply(NS(type="text", text="Okay, nothing was changed."), stop="end_turn"),
    ])
    agent = SalesAgent(conn, fake, approve=lambda n, a: False)
    assert "nothing was changed" in agent.ask("Move Ali's leads to Bilal")
    assert statuses(conn) == ["rejected_by_human"]
    # the rejection was sent back to Claude as an error tool_result
    last_user = fake.calls[1]["messages"][-1]["content"][0]
    assert last_user["is_error"] and "REJECTED" in last_user["content"]


def test_sensitive_call_approved_and_logged(conn):
    ids = open_lead_ids(conn, "Ali", 2)
    fake = FakeClient([
        reply(tool_use("reassign_leads", {"lead_ids": ids, "to_rep_name": "Bilal", "reason": "balance"})),
        reply(NS(type="text", text="Done."), stop="end_turn"),
    ])
    agent = SalesAgent(conn, fake, approve=lambda n, a: True)
    agent.ask("Move them")
    assert statuses(conn) == ["executed"]
    owner = {r[0] for r in conn.execute(
        f"SELECT r.name FROM leads l JOIN reps r ON r.id=l.rep_id WHERE l.id IN ({ids[0]},{ids[1]})")}
    assert owner == {"Bilal"}


def test_viewer_cannot_write(conn):
    fake = FakeClient([
        reply(tool_use("create_task", {"lead_id": None, "rep_name": "Sara", "title": "x", "due_in_days": 1})),
        reply(NS(type="text", text="You don't have permission."), stop="end_turn"),
    ])
    SalesAgent(conn, fake, role="viewer", approve=lambda n, a: True).ask("make a task")
    assert statuses(conn) == ["denied_by_role"]
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_parallel_tool_results_sent_in_one_message(conn):
    fake = FakeClient([
        reply(tool_use("rep_workload", {}, "a"), tool_use("data_quality_check", {}, "b")),
        reply(NS(type="text", text="ok"), stop="end_turn"),
    ])
    SalesAgent(conn, fake).ask("overview")
    results = fake.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


def test_truncated_tool_call_is_not_run(conn):
    fake = FakeClient([reply(tool_use("reassign_leads", {"lead_ids": [1]}), stop="max_tokens")])
    out = SalesAgent(conn, fake, approve=lambda n, a: True).ask("x")
    assert "cut off" in out
    assert statuses(conn) == []


def test_request_uses_fallbacks_and_strict_tools(conn):
    fake = FakeClient([reply(NS(type="text", text="hi"), stop="end_turn")])
    SalesAgent(conn, fake).ask("hello")
    kw = fake.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["fallbacks"] == "default"
    assert all(t["strict"] for t in kw["tools"])
