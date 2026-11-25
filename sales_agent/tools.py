"""CRM tools the agent can call.

Every tool has:
  * a strict JSON schema (sent to Claude),
  * a Python handler that talks to the CRM,
  * an access level: "read", "write" (logged) or "sensitive" (needs human approval).

Business rules live here, in code, so the model cannot bypass them:
  * closed leads (won/lost) cannot be reassigned,
  * a rep may hold at most MAX_OPEN_LEADS_PER_REP open leads,
  * at most MAX_BATCH leads per reassignment,
  * only allowed status transitions are accepted.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from . import crm
from .scoring import score_lead

MAX_OPEN_LEADS_PER_REP = 25
MAX_BATCH = 20
OPEN = ("new", "contacted", "interested", "negotiation")
ALLOWED_TRANSITIONS = {
    "new": {"contacted", "lost"},
    "contacted": {"interested", "lost"},
    "interested": {"negotiation", "lost", "contacted"},
    "negotiation": {"won", "lost", "interested"},
    "won": set(),
    "lost": {"contacted"},  # re-open a lost lead
}


class ToolError(Exception):
    """A rule or input problem the model should see and correct."""


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    handler: Callable[[sqlite3.Connection, dict], dict]
    access: str  # read | write | sensitive

    def spec(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": self.properties,
                "required": list(self.properties),
                "additionalProperties": False,
            },
        }


def _now() -> datetime:
    return crm.now()


def _rep_id(conn, name: str) -> int:
    row = conn.execute("SELECT id, active FROM reps WHERE lower(name)=lower(?)", (name,)).fetchone()
    if not row:
        names = [r[0] for r in conn.execute("SELECT name FROM reps ORDER BY name")]
        raise ToolError(f"Unknown rep '{name}'. Reps: {', '.join(names)}")
    if not row["active"]:
        raise ToolError(f"Rep '{name}' is not active.")
    return row["id"]


def _lead_rows(conn, where: str = "1=1", params: tuple = ()) -> list[dict]:
    return crm.rows(conn.execute(
        "SELECT l.id, l.name, l.company, l.status, l.score, l.deal_value, l.last_contact_at, "
        "l.created_at, r.name AS rep FROM leads l LEFT JOIN reps r ON r.id = l.rep_id "
        f"WHERE {where}", params))


def _days_since(ts: str | None, now: datetime) -> int | None:
    return None if not ts else (now - datetime.fromisoformat(ts)).days


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

def search_leads(conn, a: dict) -> dict:
    where, params = ["1=1"], []
    if a["status"]:
        where.append("l.status = ?")
        params.append(a["status"])
    if a["rep_name"]:
        where.append("lower(r.name) = lower(?)")
        params.append(a["rep_name"])
    if a["min_score"] is not None:
        where.append("l.score >= ?")
        params.append(a["min_score"])
    if a["inactive_days"] is not None:
        cutoff = crm.iso(_now() - timedelta(days=a["inactive_days"]))
        where.append("(l.last_contact_at IS NULL OR l.last_contact_at < ?)")
        params.append(cutoff)
    limit = max(1, min(a["limit"] or 20, 100))
    found = _lead_rows(conn, " AND ".join(where) + " ORDER BY COALESCE(l.score, -1) DESC, l.id",
                       tuple(params))
    return {"total": len(found), "leads": found[:limit]}


def get_lead(conn, a: dict) -> dict:
    found = _lead_rows(conn, "l.id = ?", (a["lead_id"],))
    if not found:
        raise ToolError(f"Lead {a['lead_id']} not found.")
    acts = crm.rows(conn.execute(
        "SELECT kind, comment, created_at FROM activities WHERE lead_id=? ORDER BY created_at",
        (a["lead_id"],)))
    tasks = crm.rows(conn.execute(
        "SELECT id, title, due_at, done FROM tasks WHERE lead_id=?", (a["lead_id"],)))
    return {"lead": found[0], "activities": acts, "tasks": tasks}


def find_neglected_leads(conn, a: dict) -> dict:
    """Open leads with no contact for N days, plus hot leads that are going cold (missed opportunities)."""
    now = _now()
    days = a["days_without_contact"]
    open_leads = _lead_rows(conn, f"l.status IN ({','.join('?' * len(OPEN))})", OPEN)
    neglected, missed = [], []
    for lead in open_leads:
        idle = _days_since(lead["last_contact_at"], now)
        lead["days_since_contact"] = idle
        if idle is None or idle >= days:
            neglected.append(lead)
            if lead["status"] in ("interested", "negotiation"):
                missed.append(lead)
    per_rep: dict[str, int] = {}
    for lead in neglected:
        per_rep[lead["rep"] or "unassigned"] = per_rep.get(lead["rep"] or "unassigned", 0) + 1
    neglected.sort(key=lambda x: (x["days_since_contact"] is not None, -(x["days_since_contact"] or 0)))
    return {
        "neglected_count": len(neglected),
        "neglected_by_rep": per_rep,
        "missed_opportunities": missed,
        "neglected_leads": neglected[:40],
    }


def rep_workload(conn, a: dict) -> dict:
    data = crm.rows(conn.execute(
        "SELECT r.name AS rep, r.active, "
        f"SUM(CASE WHEN l.status IN ({','.join('?' * len(OPEN))}) THEN 1 ELSE 0 END) AS open_leads, "
        "SUM(CASE WHEN l.status='won' THEN 1 ELSE 0 END) AS won, "
        "COUNT(l.id) AS total_leads "
        "FROM reps r LEFT JOIN leads l ON l.rep_id = r.id GROUP BY r.id ORDER BY open_leads DESC", OPEN))
    return {"max_open_leads_per_rep": MAX_OPEN_LEADS_PER_REP, "reps": data}


def score_leads(conn, a: dict) -> dict:
    now = _now()
    if a["lead_ids"]:
        ids = a["lead_ids"]
        targets = _lead_rows(conn, f"l.id IN ({','.join('?' * len(ids))})", tuple(ids))
    else:
        targets = _lead_rows(conn)
    scored = []
    for lead in targets:
        comments = [r[0] for r in conn.execute("SELECT comment FROM activities WHERE lead_id=?",
                                                (lead["id"],))]
        value = conn.execute("SELECT deal_value FROM leads WHERE id=?", (lead["id"],)).fetchone()[0]
        s, reasons = score_lead(lead["status"], comments, lead["last_contact_at"], value or 0, now)
        conn.execute("UPDATE leads SET score=? WHERE id=?", (s, lead["id"]))
        scored.append({"id": lead["id"], "name": lead["name"], "status": lead["status"],
                       "rep": lead["rep"], "score": s, "reasons": reasons})
    conn.commit()
    scored.sort(key=lambda x: -x["score"])
    return {"scored": len(scored), "top": scored[:15],
            "rules": "status base + comment keywords (EN/AR) + recency + deal value"}


def performance_report(conn, a: dict) -> dict:
    now = _now()
    span = {"daily": 1, "weekly": 7, "monthly": 30}[a["period"]]
    since = crm.iso(now - timedelta(days=span))
    reps = []
    for rep in crm.rows(conn.execute("SELECT id, name FROM reps WHERE active=1 ORDER BY name")):
        rid = rep["id"]
        q = lambda sql, *p: conn.execute(sql, p).fetchone()[0] or 0  # noqa: E731
        new_leads = q("SELECT COUNT(*) FROM leads WHERE rep_id=? AND created_at>=?", rid, since)
        contacts = q("SELECT COUNT(*) FROM activities WHERE rep_id=? AND created_at>=?", rid, since)
        won = q("SELECT COUNT(*) FROM leads WHERE rep_id=? AND status='won'", rid)
        won_value = q("SELECT SUM(deal_value) FROM leads WHERE rep_id=? AND status='won'", rid)
        closed = q("SELECT COUNT(*) FROM leads WHERE rep_id=? AND status IN ('won','lost')", rid)
        open_ = q(f"SELECT COUNT(*) FROM leads WHERE rep_id=? AND status IN ({','.join('?' * len(OPEN))})",
                  rid, *OPEN)
        reps.append({
            "rep": rep["name"], "new_leads_in_period": new_leads, "contacts_in_period": contacts,
            "open_leads": open_, "won_total": won, "won_value_total": round(won_value, 2),
            "win_rate_of_closed": f"{(won / closed * 100):.0f}%" if closed else "n/a",
        })
    neglected = find_neglected_leads(conn, {"days_without_contact": 7})
    return {
        "period": a["period"], "since": since, "generated_at": crm.iso(now), "reps": reps,
        "neglected_7d": neglected["neglected_count"],
        "missed_opportunities": len(neglected["missed_opportunities"]),
        "data_quality": data_quality_check(conn, {})["issues"],
    }


def data_quality_check(conn, a: dict) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    issues = {
        "leads_without_rep": q("SELECT COUNT(*) FROM leads WHERE rep_id IS NULL"),
        "leads_without_phone": q("SELECT COUNT(*) FROM leads WHERE phone IS NULL OR phone=''"),
        "open_leads_never_contacted": q(
            "SELECT COUNT(*) FROM leads WHERE last_contact_at IS NULL AND status NOT IN ('new','won','lost')"),
        "status_new_older_than_7_days": q(
            f"SELECT COUNT(*) FROM leads WHERE status='new' AND created_at < '{crm.iso(_now() - timedelta(days=7))}'"),
        "duplicate_phone_numbers": q(
            "SELECT COUNT(*) FROM (SELECT phone FROM leads GROUP BY phone HAVING COUNT(*) > 1)"),
    }
    return {"issues": issues}


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

def create_task(conn, a: dict) -> dict:
    rid = _rep_id(conn, a["rep_name"])
    if a["lead_id"] is not None and not _lead_rows(conn, "l.id=?", (a["lead_id"],)):
        raise ToolError(f"Lead {a['lead_id']} not found.")
    due = crm.iso(_now() + timedelta(days=max(0, min(a["due_in_days"], 60))))
    cur = conn.execute("INSERT INTO tasks(lead_id, rep_id, title, due_at, created_at) VALUES (?,?,?,?,?)",
                       (a["lead_id"], rid, a["title"][:200], due, crm.iso(_now())))
    conn.commit()
    return {"task_id": cur.lastrowid, "rep": a["rep_name"], "due_at": due}


def reassign_leads(conn, a: dict) -> dict:
    ids = list(dict.fromkeys(a["lead_ids"]))
    if not ids:
        raise ToolError("lead_ids is empty.")
    if len(ids) > MAX_BATCH:
        raise ToolError(f"At most {MAX_BATCH} leads per reassignment.")
    to_id = _rep_id(conn, a["to_rep_name"])
    leads = _lead_rows(conn, f"l.id IN ({','.join('?' * len(ids))})", tuple(ids))
    missing = set(ids) - {lead["id"] for lead in leads}
    if missing:
        raise ToolError(f"Leads not found: {sorted(missing)}")
    closed = [lead["id"] for lead in leads if lead["status"] not in OPEN]
    if closed:
        raise ToolError(f"Closed leads cannot be reassigned: {closed}")
    moving = [lead for lead in leads if lead["rep"] != a["to_rep_name"]]
    current = conn.execute(
        f"SELECT COUNT(*) FROM leads WHERE rep_id=? AND status IN ({','.join('?' * len(OPEN))})",
        (to_id, *OPEN)).fetchone()[0]
    if current + len(moving) > MAX_OPEN_LEADS_PER_REP:
        raise ToolError(f"{a['to_rep_name']} would have {current + len(moving)} open leads "
                        f"(limit {MAX_OPEN_LEADS_PER_REP}).")
    for lead in moving:
        conn.execute("UPDATE leads SET rep_id=? WHERE id=?", (to_id, lead["id"]))
        conn.execute("INSERT INTO activities(lead_id, rep_id, kind, comment, created_at) VALUES (?,?,?,?,?)",
                     (lead["id"], to_id, "note",
                      f"Reassigned from {lead['rep']} to {a['to_rep_name']}: {a['reason']}", crm.iso(_now())))
    conn.commit()
    return {"reassigned": [lead["id"] for lead in moving], "to": a["to_rep_name"],
            "skipped_already_assigned": [lead["id"] for lead in leads if lead not in moving]}


def update_lead_status(conn, a: dict) -> dict:
    found = _lead_rows(conn, "l.id=?", (a["lead_id"],))
    if not found:
        raise ToolError(f"Lead {a['lead_id']} not found.")
    old = found[0]["status"]
    new = a["new_status"]
    if new not in ALLOWED_TRANSITIONS[old]:
        raise ToolError(f"Status change {old} -> {new} is not allowed. "
                        f"Allowed: {sorted(ALLOWED_TRANSITIONS[old]) or 'none'}")
    conn.execute("UPDATE leads SET status=? WHERE id=?", (new, a["lead_id"]))
    conn.execute("INSERT INTO activities(lead_id, kind, comment, created_at) VALUES (?,?,?,?)",
                 (a["lead_id"], "status_change", f"{old} -> {new}: {a['note']}", crm.iso(_now())))
    conn.commit()
    return {"lead_id": a["lead_id"], "from": old, "to": new}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

NULL_INT = {"type": ["integer", "null"]}
STATUS_ENUM = {"type": "string", "enum": list(crm.STATUSES)}

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("search_leads",
         "Search CRM leads. Use null for any filter you don't need. Results are sorted by score.",
         {"status": {"type": ["string", "null"], "enum": [*crm.STATUSES, None]},
          "rep_name": {"type": ["string", "null"]},
          "min_score": NULL_INT,
          "inactive_days": {**NULL_INT, "description": "Only leads with no contact for at least this many days"},
          "limit": {**NULL_INT, "description": "Max leads to return (default 20, max 100)"}},
         search_leads, "read"),
    Tool("get_lead", "Get one lead with its full activity history (comments may be English or Arabic) and tasks.",
         {"lead_id": {"type": "integer"}}, get_lead, "read"),
    Tool("find_neglected_leads",
         "Find open leads with no contact for N days, and missed opportunities "
         "(interested/negotiation leads going cold).",
         {"days_without_contact": {"type": "integer"}}, find_neglected_leads, "read"),
    Tool("rep_workload", "Open/won/total leads per sales rep and the max open leads allowed per rep.",
         {}, rep_workload, "read"),
    Tool("score_leads",
         "Score leads 0-100 with the company's fixed rules (status, EN/AR comment keywords, recency, deal value) "
         "and save the scores. Pass null to score all leads.",
         {"lead_ids": {"type": ["array", "null"], "items": {"type": "integer"}}}, score_leads, "read"),
    Tool("performance_report", "Sales performance per rep for a period, plus neglected leads and data quality.",
         {"period": {"type": "string", "enum": ["daily", "weekly", "monthly"]}}, performance_report, "read"),
    Tool("data_quality_check", "Count CRM data quality problems (missing rep/phone, duplicates, stale statuses).",
         {}, data_quality_check, "read"),
    Tool("create_task", "Create a follow-up task for a rep. lead_id may be null for a general task.",
         {"lead_id": NULL_INT, "rep_name": {"type": "string"}, "title": {"type": "string"},
          "due_in_days": {"type": "integer"}}, create_task, "write"),
    Tool("reassign_leads",
         "Move open leads to another rep. SENSITIVE: a manager must approve before it runs. "
         f"Max {MAX_BATCH} leads per call; reps may not exceed {MAX_OPEN_LEADS_PER_REP} open leads.",
         {"lead_ids": {"type": "array", "items": {"type": "integer"}}, "to_rep_name": {"type": "string"},
          "reason": {"type": "string"}}, reassign_leads, "sensitive"),
    Tool("update_lead_status",
         "Change a lead's status following the allowed pipeline transitions. SENSITIVE: needs manager approval.",
         {"lead_id": {"type": "integer"}, "new_status": STATUS_ENUM, "note": {"type": "string"}},
         update_lead_status, "sensitive"),
]}


def validate(tool: Tool, args: dict) -> None:
    """Light runtime check on top of strict mode (defence in depth for a manual loop)."""
    if not isinstance(args, dict):
        raise ToolError("Input must be an object.")
    expected = set(tool.properties)
    if set(args) != expected:
        raise ToolError(f"Expected fields {sorted(expected)}, got {sorted(args)}.")


def run_tool(conn, name: str, args: dict) -> str:
    tool = TOOLS[name]
    validate(tool, args)
    return json.dumps(tool.handler(conn, args), ensure_ascii=False, default=str)
