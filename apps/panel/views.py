from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .services.spend import mtd_spend_by_locksmith


@login_required
def spend(request):
    return render(request, "panel/spend.html", {"rows": mtd_spend_by_locksmith()})
