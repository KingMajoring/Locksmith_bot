"""Display-label overrides for Handl's raw loss_type values, per the
business's own terminology (e.g. "AKL" for what Handl calls "Lost").
Anything not listed here is shown as-is.
"""
from __future__ import annotations

_LOSS_TYPE_LABELS = {
    "LOCKED IN PROPERTY": "Gain access",
    "LOST": "AKL",
}


def display_loss_type(raw: str) -> str:
    if not raw:
        return raw
    return _LOSS_TYPE_LABELS.get(raw.strip().upper(), raw)


def raw_values_for_display_label(label: str) -> list[str]:
    """Inverse of display_loss_type() — every raw Handl value that maps
    to this display label, for filtering CompletedJob.loss_type by the
    label shown in reports (falls back to the label itself for values
    with no override, since those pass through unchanged)."""
    matches = [raw for raw, mapped in _LOSS_TYPE_LABELS.items() if mapped == label]
    return matches or [label]
