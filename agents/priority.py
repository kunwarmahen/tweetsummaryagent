"""Important ("VIP") accounts — highlight colors and lookups.

An account marked important gets a distinct color (auto-assigned from a palette, overridable).
Its tweets are highlighted in the digest, guaranteed inclusion, and floated to the top.
"""
from __future__ import annotations

# Curated, visually distinct, reasonably accessible palette.
PALETTE = [
    "#1d9bf0", "#e0245e", "#17bf63", "#f45d22", "#794bc4",
    "#ffad1f", "#e91e63", "#00b8d4", "#8b5cf6", "#0f9d58",
]
DEFAULT_COLOR = PALETTE[0]


def pick_color(used: list[str]) -> str:
    """Next palette color not already in use (cycles once the palette is exhausted)."""
    for c in PALETTE:
        if c not in used:
            return c
    return PALETTE[len(used) % len(PALETTE)]


def load_important() -> dict[str, str]:
    """Return {handle_lowercased: color} for all accounts marked important."""
    from sqlmodel import select

    from db.models import AccountSetting
    from db.session import get_session

    with get_session() as session:
        rows = session.exec(select(AccountSetting).where(AccountSetting.important == True)).all()  # noqa: E712
    return {r.handle.lower(): (r.color or DEFAULT_COLOR) for r in rows}
