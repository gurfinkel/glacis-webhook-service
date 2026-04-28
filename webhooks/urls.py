from django.urls import path

from webhooks import views
from webhooks.paths import INGEST_PATH

urlpatterns = [
    path(INGEST_PATH.lstrip("/"), views.IngestWebhookView.as_view(), name="ingest-webhook"),
    path("health", views.HealthView.as_view(), name="health"),
    path("health/live", views.LivenessView.as_view(), name="liveness"),
]
