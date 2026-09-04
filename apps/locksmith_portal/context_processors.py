"""Panic button (see templates/locksmith_portal/base.html) — needs the
primary safety contact's phone number on every portal page, not just
one view's context, so it's a context processor rather than per-view
context (same pattern as
apps.job_completion.context_processors.needs_categorization_count).
"""
from __future__ import annotations


def panic_contact(request):
    if not request.path.startswith("/locksmith/") or not request.user.is_authenticated:
        return {}
    from .models import SeniorStaffContact

    primary = SeniorStaffContact.objects.filter(active=True).order_by("order", "name").first()
    return {"panic_contact_phone": primary.phone_number if primary else ""}
