from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path(
        "",
        login_required(RedirectView.as_view(pattern_name="stock_accuracy:dashboard")),
        name="home",
    ),
    path("stock-accuracy/", include("apps.stock_accuracy.urls")),
    path("job-completion/", include("apps.job_completion.urls")),
    path("locksmiths/", include("apps.locksmiths.urls")),
    path("locksmith/", include("apps.locksmith_portal.urls")),
]
