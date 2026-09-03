from django.urls import path

from . import views

app_name = "locksmith_portal"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("stock-check/<int:pk>/", views.stock_check_entry, name="stock_check_entry"),
    path("jobs/<path:order_no>/", views.job_detail, name="job_detail"),
]
