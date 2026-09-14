from django import template

register = template.Library()


@register.filter
def drive_time_class(duration_minutes):
    """CSS class for a "Nearest locksmiths" drive-time pill, per the
    office's own thresholds: 0-45 min green (comfortably close), 46-60
    amber, 61-90 pale red, over 90 red (probably not worth sending this
    locksmith)."""
    if duration_minutes is None:
        return ""
    if duration_minutes <= 45:
        return "drive-time-green"
    if duration_minutes <= 60:
        return "drive-time-amber"
    if duration_minutes <= 90:
        return "drive-time-pale-red"
    return "drive-time-red"
