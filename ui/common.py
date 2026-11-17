"""Labels and formatting for sourced values."""

from __future__ import annotations

EDU_SHORT = {
    "all": "Not specified (all)",
    "below_upper_secondary": "No high-school diploma",
    "upper_secondary": "High school",
    "short_tertiary": "Associate's",
    "bachelor": "Bachelor's",
    "master_plus": "Master's or higher",
}


def src(s, fmt="{:,.0f}") -> str:
    """Help text for a sourced default."""
    try:
        v = fmt.format(s.value)
    except (TypeError, ValueError):
        v = str(s.value)
    est = " ⚠️ ESTIMATE" if s.estimated else ""
    return (
        f"Default {v} {s.unit} — {s.source}"
        + (f" ({s.year})" if s.year else "")
        + (f", quality {s.quality}" if s.quality else "")
        + est
        + (f". {s.note}" if s.note else "")
    )


def flag(s) -> str:
    return " ⚠️" if s.estimated else ""
