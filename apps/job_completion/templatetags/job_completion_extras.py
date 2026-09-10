from django import template
from django.conf import settings

register = template.Library()


@register.filter
def handl_claim_url(report_id):
    """Direct link to this claim in Handl's own UI (see
    settings.HANDL_CLAIM_URL_TEMPLATE) — lets office jump straight from
    an order/report row in our own reports to the real claim, instead
    of pasting the ReportID into Handl's own search by hand."""
    if not report_id:
        return ""
    return settings.HANDL_CLAIM_URL_TEMPLATE.format(report_id=report_id)
