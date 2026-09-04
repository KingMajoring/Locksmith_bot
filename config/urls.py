from django.conf import settings
from django.conf.urls.static import static
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
    path("panel/", include("apps.panel.urls")),
]

if settings.DEBUG:
    # MockPhotoStorage-only — production always uses Azure Blob Storage
    # (AZURE_STORAGE_CONNECTION_STRING set), never serves media locally.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
