"""Command line for the AI Sales Management Agent.

  python cli.py init                     create and fill the demo CRM
  python cli.py chat [--role viewer]     talk to the agent (needs ANTHROPIC_API_KEY)
  python cli.py ask "question"           one question, then exit
  python cli.py report --period weekly   scheduled report, no AI needed (use with cron)
  python cli.py audit [--limit 20]       show the audit log
"""

from __future__ import annotations

import argparse
import json
import sys

from sales_agent import crm
from sales_agent.tools import performance_report

BOLD, DIM, CYAN, YELLOW, GREEN, RED, RESET = (
    "\033[1m", "\033[2m", "\033[36m", "\033[33m", "\033[32m", "\033[31m", "\033[0m")


def ask_approval(name: str, args: dict) -> bool:
    print(f"\n{YELLOW}{BOLD}⚠ Approval needed:{RESET} {name}")
    print(f"{YELLOW}{json.dumps(args, ensure_ascii=False, indent=2)}{RESET}")
    answer = input(f"{BOLD}Approve? [y/N] {RESET}").strip().lower()
    print(GREEN + "✔ approved" + RESET if answer == "y" else RED + "✘ rejected" + RESET)
    return answer == "y"


def show_event(kind: str, text: str) -> None:
    if kind == "tool":
        print(f"{DIM}  → {text}{RESET}")


def make_agent(role: str):
    from sales_agent.agent import SalesAgent  # imported here so report/audit work without the SDK
    conn = crm.connect()
    crm.seed(conn)
    return SalesAgent(conn, role=role, approve=ask_approval, on_event=show_event)


def cmd_chat(args) -> None:
    agent = make_agent(args.role)
    print(f"{BOLD}AI Sales Manager{RESET} (role: {args.role}). Type 'exit' to quit.\n")
    while True:
        try:
            q = input(f"{CYAN}{BOLD}you ›{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in ("exit", "quit"):
            break
        if q:
            print(f"\n{GREEN}{BOLD}agent ›{RESET} {agent.ask(q)}\n")


def cmd_ask(args) -> None:
    print(make_agent(args.role).ask(args.question))


def cmd_report(args) -> None:
    conn = crm.connect()
    crm.seed(conn)
    r = performance_report(conn, {"period": args.period})
    print(f"{BOLD}Sales report – {r['period']}{RESET}  (since {r['since'][:10]})\n")
    cols = ["rep", "new_leads_in_period", "contacts_in_period", "open_leads", "won_total",
            "won_value_total", "win_rate_of_closed"]
    heads = ["Rep", "New", "Contacts", "Open", "Won", "Won value", "Win rate"]
    widths = [max(len(h), *(len(str(x[c])) for x in r["reps"])) for h, c in zip(heads, cols)]
    print("  ".join(h.ljust(w) for h, w in zip(heads, widths)))
    print("  ".join("-" * w for w in widths))
    for x in r["reps"]:
        print("  ".join(str(x[c]).ljust(w) for c, w in zip(cols, widths)))
    print(f"\nNeglected leads (7+ days): {r['neglected_7d']}   "
          f"Missed opportunities: {r['missed_opportunities']}")
    print("Data quality:", ", ".join(f"{k}={v}" for k, v in r["data_quality"].items()))


def cmd_audit(args) -> None:
    conn = crm.connect()
    for row in conn.execute("SELECT at, actor, tool, status, input FROM audit_log ORDER BY id DESC LIMIT ?",
                            (args.limit,)):
        color = GREEN if row["status"] == "executed" else (YELLOW if row["status"] == "invalid_input" else RED)
        print(f"{row['at'][:19]}  {row['actor']:<14} {row['tool']:<20} {color}{row['status']:<18}{RESET} "
              f"{DIM}{row['input'][:70]}{RESET}")


def cmd_init(args) -> None:
    conn = crm.connect()
    crm.seed(conn)
    n = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    print(f"Demo CRM ready: {n} leads in {crm.DEFAULT_DB.name}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="AI Sales Management Agent (Claude tool calling demo)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    c = sub.add_parser("chat")
    c.add_argument("--role", choices=["manager", "viewer"], default="manager")
    c.set_defaults(fn=cmd_chat)
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--role", choices=["manager", "viewer"], default="manager")
    a.set_defaults(fn=cmd_ask)
    r = sub.add_parser("report")
    r.add_argument("--period", choices=["daily", "weekly", "monthly"], default="weekly")
    r.set_defaults(fn=cmd_report)
    au = sub.add_parser("audit")
    au.add_argument("--limit", type=int, default=20)
    au.set_defaults(fn=cmd_audit)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
