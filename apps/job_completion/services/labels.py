"""Display-label overrides for Handl's raw loss_type values, per the
business's own terminology (e.g. "AKL" for what Handl calls "Lost").
Anything not listed here is shown as-is.
"""
from __future__ import annotations

_LOSS_TYPE_LABELS = {
    "LOCKED IN PROPERTY": "Gain access",
    "LOST": "AKL",
}


def display_loss_type(raw: str, *, spare_key: bool | None = None) -> str:
    """spare_key is Handl's Policy_KeyClaims.SpareKey for this specific
    job (see JobDetails.spare_key) — only meaningful for a "LOST" raw
    value, where it decides between the two real-world cases Handl
    files under that one loss_type: genuinely no spare key anywhere
    ("AKL") vs. a spare still exists somewhere ("Spare Key", the same
    label already used for jobs Handl itself types that way). Omit it
    (the default) to get the old behaviour, for a caller with no
    per-job spare_key to hand — e.g. CompletedJob reporting/
    benchmarking, which doesn't capture this flag at all."""
    if not raw:
        return raw
    normalized = raw.strip().upper()
    if normalized == "LOST" and spare_key is True:
        return "Spare Key"
    return _LOSS_TYPE_LABELS.get(normalized, raw)


def raw_values_for_display_label(label: str) -> list[str]:
    """Inverse of display_loss_type() — every raw Handl value that maps
    to this display label, for filtering CompletedJob.loss_type by the
    label shown in reports (falls back to the label itself for values
    with no override, since those pass through unchanged)."""
    matches = [raw for raw, mapped in _LOSS_TYPE_LABELS.items() if mapped == label]
    return matches or [label]


_GAIN_ACCESS_RAW_VALUES = {v.upper() for v in raw_values_for_display_label("Gain access")}


def is_gain_access_loss_type(raw: str) -> bool:
    """True if this raw Handl loss_type value is one that displays as
    "Gain access" — a lock-out/break-in job, where (unlike most other
    services) finishing with no parts disposed, or in well under a
    typical duration, can be perfectly genuine."""
    return (raw or "").strip().upper() in _GAIN_ACCESS_RAW_VALUES
