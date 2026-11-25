"""Deterministic lead scoring rules.

The AI never invents a score: it calls the score_leads tool, which applies these rules.
That keeps decisions explainable and consistent with the business rules.
Comments in English and Arabic are both understood.
"""

from __future__ import annotations

from datetime import datetime

STATUS_POINTS = {"new": 20, "contacted": 30, "interested": 55, "negotiation": 70, "won": 100, "lost": 0}

POSITIVE_WORDS = [
    "interested", "quotation", "quote", "demo", "budget", "ready", "sign", "contract", "meeting",
    "مهتم", "عرض سعر", "موعد", "اجتماع", "جاهز", "عقد",
]
NEGATIVE_WORDS = [
    "not interested", "too high", "competitor", "no answer", "switched off", "wrong number",
    "غير مهتم", "الرقم خطأ", "لا يرد", "منافس",
]


def comment_signal(comments: list[str]) -> int:
    """Points from activity comments: +6 per positive phrase, -8 per negative phrase (capped)."""
    pts = 0
    for c in comments:
        text = (c or "").lower()
        neg = any(w in text for w in NEGATIVE_WORDS)
        if neg:
            pts -= 8
            continue  # "not interested" must not also count as "interested"
        if any(w in text for w in POSITIVE_WORDS):
            pts += 6
    return max(-25, min(25, pts))


def recency_signal(last_contact_at: str | None, now: datetime) -> int:
    if not last_contact_at:
        return -10
    days = (now - datetime.fromisoformat(last_contact_at)).days
    if days <= 3:
        return 10
    if days <= 7:
        return 5
    if days <= 14:
        return 0
    return -10


def score_lead(status: str, comments: list[str], last_contact_at: str | None, deal_value: float,
               now: datetime) -> tuple[int, list[str]]:
    """Return (score 0-100, reasons) for one lead."""
    reasons = []
    base = STATUS_POINTS.get(status, 20)
    reasons.append(f"status '{status}': {base}")
    c = comment_signal(comments)
    if c:
        reasons.append(f"comments: {c:+d}")
    r = recency_signal(last_contact_at, now)
    if r:
        reasons.append(f"last contact: {r:+d}")
    v = 5 if deal_value >= 10000 else 0
    if v:
        reasons.append("deal value >= 10,000: +5")
    if status in ("won", "lost"):
        return base, reasons  # closed leads keep their status score
    return max(0, min(100, base + c + r + v)), reasons
