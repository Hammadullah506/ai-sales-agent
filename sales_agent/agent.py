"""The AI management assistant: a Claude tool-calling loop over the CRM tools.

Safety layers (in order):
  1. Role permissions  - a "viewer" can only use read tools.
  2. Business rules    - enforced inside each tool (see tools.py), never left to the model.
  3. Human approval    - every "sensitive" tool call waits for a manager's yes/no.
  4. Audit log         - every call (executed, rejected, denied or failed) is written to audit_log.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable

import anthropic

from . import crm
from .tools import TOOLS, ToolError, run_tool

MODEL = "claude-opus-5-5"
MAX_TURNS = 15

SYSTEM_PROMPT = """You are the AI management assistant for a call-center sales team.
You work only through the CRM tools you are given. Never invent leads, numbers or names:
if you need data, call a tool.

How to work:
- Lead scores come only from the score_leads tool (the company's fixed rules). Do not make up scores.
- Before reassigning leads, check rep_workload so work is spread fairly, and give a clear reason.
- reassign_leads and update_lead_status are sensitive: a manager approves or rejects each call.
  If a call is rejected, do not retry it in another form; tell the user and suggest a next step.
- If a tool returns an error (a business rule), explain it plainly and adjust.
- Activity comments may be in Arabic or English. Reply in the language the user writes in.
- Keep answers short and practical: a small table or bullet list, then the recommended action."""

Approver = Callable[[str, dict], bool]
Printer = Callable[[str, str], None]


class SalesAgent:
    def __init__(self, conn: sqlite3.Connection, client: anthropic.Anthropic | None = None, *,
                 role: str = "manager", user: str = "demo", approve: Approver | None = None,
                 on_event: Printer | None = None, effort: str = "medium", model: str = MODEL):
        if role not in ("manager", "viewer"):
            raise ValueError("role must be 'manager' or 'viewer'")
        self.conn = conn
        self.client = client or anthropic.Anthropic()
        self.role = role
        self.actor = f"{role}:{user}"
        self.approve = approve or (lambda name, args: False)  # safe default: reject
        self.on_event = on_event or (lambda kind, text: None)
        self.effort = effort
        self.model = model
        self.messages: list = []
        self.tool_specs = [t.spec() for t in TOOLS.values()]

    # -- one tool call -------------------------------------------------------
    def _execute(self, name: str, args: dict) -> tuple[str, bool]:
        """Return (content, is_error) and write the audit log."""
        tool = TOOLS.get(name)
        if tool is None:
            crm.log_audit(self.conn, self.actor, name, args, "error", "unknown tool")
            return f"Unknown tool {name}", True

        if self.role == "viewer" and tool.access != "read":
            crm.log_audit(self.conn, self.actor, name, args, "denied_by_role", "viewer cannot write")
            return "Permission denied: your role (viewer) can only read data.", True

        if tool.access == "sensitive":
            self.on_event("approval", f"{name} {json.dumps(args, ensure_ascii=False)}")
            if not self.approve(name, args):
                crm.log_audit(self.conn, self.actor, name, args, "rejected_by_human", None)
                return "The manager REJECTED this action. It was not executed.", True

        try:
            out = run_tool(self.conn, name, args)
        except ToolError as e:
            crm.log_audit(self.conn, self.actor, name, args, "invalid_input", str(e))
            return f"Rule/input error: {e}", True
        except Exception as e:  # unexpected bug: record it, let the model report it
            crm.log_audit(self.conn, self.actor, name, args, "error", repr(e))
            return f"Internal error while running {name}.", True

        crm.log_audit(self.conn, self.actor, name, args, "executed", out[:300])
        return out, False

    # -- the loop ------------------------------------------------------------
    def ask(self, text: str) -> str:
        self.messages.append({"role": "user", "content": text})
        for _ in range(MAX_TURNS):
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=self.tool_specs,
                messages=self.messages,
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            # Keep the full content (thinking + tool_use blocks), not just the text.
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                return "The model declined this request."
            if response.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "max_tokens":
                if tool_uses:  # a cut-off tool call may be incomplete: never run it
                    return "The response was cut off before a tool call finished. Please try again."
                return _text(response) + "\n[cut off]"
            if not tool_uses:
                return _text(response)

            results = []
            for block in tool_uses:
                self.on_event("tool", f"{block.name}({json.dumps(block.input, ensure_ascii=False)})")
                content, is_error = self._execute(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content, "is_error": is_error})
            # All results go back in ONE user message.
            self.messages.append({"role": "user", "content": results})

        return "Stopped: too many steps for one request."


def _text(response) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text").strip()
