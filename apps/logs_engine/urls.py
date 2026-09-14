from django.urls import path

from . import views

app_name = "logs_engine"

urlpatterns = [
    path("", views.lookup, name="lookup"),
]
