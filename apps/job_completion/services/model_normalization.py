"""Collapses a raw Handl vehicle model string down to a broad model
family, so failure analysis groups "CORSA STING" and "CORSA SE AUTO"
together as one thing rather than treating every trim/spec variant as
a separate model.

The general rule (confirmed with the business: "ignore the last bit of
the model — this can be followed on all models really") is just the
first word of the model string. Two manufacturers use numeric/letter
codes instead of a word as their actual model name, so they need their
own rule to land on the same family the business actually means:

- BMW: a 3-digit code's first digit is the series (335D, 330i, 320d
  all -> "3 Series"; 430 -> "4 Series"). X/i/M-prefixed names (X5, i3,
  M340i) already read as their own family as-is, so they fall through
  to the default rule unchanged.
- Mercedes-Benz: a leading class-letter code names the family (C220,
  C300 AMG Line -> "C-Class"; E250 -> "E-Class"). Multi-letter codes
  (CLA, GLC, GLE, ...) are checked before their single-letter prefixes
  so "CLA45" doesn't get mistaken for a "C".

Add more manufacturer-specific rules here as real data surfaces cases
the default rule doesn't handle well — same iterative approach used
for the rest of the Handl integration.
"""
from __future__ import annotations

import re

_BMW_SERIES_RE = re.compile(r"^([1-8])\d{2}[A-Za-z]*$")

# Longer codes first — Python's re tries alternatives left-to-right and
# stops at the first match, so a single-letter code listed before a
# multi-letter one it prefixes (e.g. "C" before "CLA") would shadow it.
_MERC_CLASS_RE = re.compile(
    r"^(CLA|CLS|CLK|GLA|GLB|GLC|GLE|GLS|GLK|SLK|SLC|ML|GL|A|B|C|E|G|R|S|V)"
)


def normalize_model(make: str, model: str) -> str:
    model = (model or "").strip()
    if not model:
        return ""
    first_word = model.split()[0].upper()
    make_upper = (make or "").strip().upper()

    if make_upper == "BMW":
        match = _BMW_SERIES_RE.match(first_word)
        if match:
            return f"{match.group(1)} Series"
        return first_word

    if "MERCEDES" in make_upper:
        match = _MERC_CLASS_RE.match(first_word)
        if match:
            return f"{match.group(1)}-Class"
        return first_word

    return first_word
