"""A small demo CRM stored in SQLite: sales reps, leads, activity comments, tasks and an audit log.

In a real project this module would be replaced by calls to the client's CRM API.
The agent only talks to the CRM through the functions in tools.py.
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "crm_demo.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reps (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    company TEXT,
    phone TEXT,
    source TEXT,
    status TEXT NOT NULL,            -- new, contacted, interested, negotiation, won, lost
    rep_id INTEGER REFERENCES reps(id),
    deal_value REAL DEFAULT 0,
    score INTEGER,                   -- 0-100, filled by the scoring rules
    created_at TEXT NOT NULL,
    last_contact_at TEXT
);
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    rep_id INTEGER REFERENCES reps(id),
    kind TEXT NOT NULL,              -- call, note, email, status_change
    comment TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id),
    rep_id INTEGER REFERENCES reps(id),
    title TEXT NOT NULL,
    due_at TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,             -- who asked (role:user)
    tool TEXT NOT NULL,
    input TEXT NOT NULL,             -- JSON
    status TEXT NOT NULL,            -- executed, rejected_by_human, denied_by_role, invalid_input, error
    result TEXT                      -- short summary
);
"""

STATUSES = ["new", "contacted", "interested", "negotiation", "won", "lost"]


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------

REP_NAMES = ["Ali", "Sara", "Omar", "Ayesha", "Bilal"]

FIRST = ["Ahmed", "Fatima", "Khalid", "Noor", "Hassan", "Mariam", "Yusuf", "Layla", "Imran", "Hina",
         "Tariq", "Zainab", "Faisal", "Amna", "Saad", "Rania", "Kamran", "Huda", "Usman", "Salma"]
COMPANIES = ["Gulf Trading", "Desert Logistics", "Blue Ocean Foods", "Al Noor Clinics", "Skyline Realty",
             "Falcon Motors", "Crescent Retail", "Oasis Travel", "Pearl Electronics", "Atlas Builders"]
SOURCES = ["website", "facebook_ads", "referral", "google_ads", "walk_in"]

POSITIVE = [
    "Customer is very interested, asked for a quotation",
    "Wants a demo next week, budget approved",
    "Ready to sign, waiting for contract",
    "العميل مهتم جداً ويريد عرض سعر",          # customer very interested, wants a quote
    "طلب موعد للاجتماع الأسبوع القادم",          # asked for a meeting next week
]
NEUTRAL = [
    "Called, asked to call back later",
    "Sent brochure by email",
    "Needs to discuss with manager",
    "تم إرسال التفاصيل عبر الواتساب",           # details sent on WhatsApp
]
NEGATIVE = [
    "Not interested, price too high",
    "No answer, phone switched off",
    "Chose a competitor",
    "غير مهتم حالياً",                          # not interested right now
    "الرقم خطأ",                                # wrong number
]


def seed(conn: sqlite3.Connection, n_leads: int = 60, rng_seed: int = 7) -> None:
    """Fill an empty database with realistic demo data (deterministic for a given seed)."""
    if conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]:
        return
    rng = random.Random(rng_seed)
    t0 = now()
    conn.executemany("INSERT INTO reps(name) VALUES (?)", [(n,) for n in REP_NAMES])
    rep_ids = [r[0] for r in conn.execute("SELECT id FROM reps ORDER BY id")]

    for i in range(n_leads):
        created = t0 - timedelta(days=rng.randint(1, 60), hours=rng.randint(0, 23))
        status = rng.choices(STATUSES, weights=[14, 22, 18, 10, 8, 10])[0]
        # Reps 1-2 are overloaded, which gives the agent something to rebalance.
        rep = rng.choices(rep_ids, weights=[30, 28, 15, 15, 12])[0]
        n_acts = 0 if status == "new" else rng.randint(1, 4)
        last_contact = None
        cur = conn.execute(
            "INSERT INTO leads(name, company, phone, source, status, rep_id, deal_value, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"{rng.choice(FIRST)} {chr(65 + i % 26)}.", rng.choice(COMPANIES),
             f"+971-5{rng.randint(0, 9)}-{rng.randint(1000000, 9999999)}", rng.choice(SOURCES),
             status, rep, round(rng.uniform(500, 15000), -1), iso(created)),
        )
        lead_id = cur.lastrowid
        for _ in range(n_acts):
            age = (t0 - created).days
            when = created + timedelta(days=rng.randint(0, max(age, 0)), hours=rng.randint(0, 8))
            pool = POSITIVE if status in ("interested", "negotiation", "won") else (
                NEGATIVE if status == "lost" else NEUTRAL)
            conn.execute(
                "INSERT INTO activities(lead_id, rep_id, kind, comment, created_at) VALUES (?,?,?,?,?)",
                (lead_id, rep, rng.choice(["call", "note", "email"]), rng.choice(pool), iso(when)),
            )
            last_contact = max(last_contact or when, when)
        conn.execute("UPDATE leads SET last_contact_at=? WHERE id=?",
                     (iso(last_contact) if last_contact else None, lead_id))
    conn.commit()


def log_audit(conn: sqlite3.Connection, actor: str, tool: str, tool_input: dict, status: str,
              result: str | None) -> None:
    conn.execute(
        "INSERT INTO audit_log(at, actor, tool, input, status, result) VALUES (?,?,?,?,?,?)",
        (iso(now()), actor, tool, json.dumps(tool_input, ensure_ascii=False), status,
         (result or "")[:500]),
    )
    conn.commit()
