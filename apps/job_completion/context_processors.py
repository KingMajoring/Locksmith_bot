"""Sidebar notification badge (see templates/base.html) — needs the
outstanding "needs categorization" count on every page, not just the
Job Failures page itself, so it's a context processor rather than
per-view context.
"""
from __future__ import annotations


def needs_categorization_count(request):
    if not request.user.is_authenticated:
        return {}
    from .services.reporting import needs_categorization_queryset

    return {"needs_categorization_count": needs_categorization_queryset().count()}


def unreviewed_disposal_edits_count(request):
    if not request.user.is_authenticated:
        return {}
    from apps.locksmith_portal.models import PortalDisposalEdit

    return {
        "unreviewed_disposal_edits_count": PortalDisposalEdit.objects.filter(
            reviewed_at__isnull=True
        ).count()
    }
