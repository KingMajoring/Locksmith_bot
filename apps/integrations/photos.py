"""Photo storage for the locksmith portal's on-route/arrived/completed
job tracking (see apps.locksmith_portal.views/models.JobVisit).

Handl has no way to receive these directly: the "Add Note" file upload
has no API (confirmed live), and the file it saves isn't retrievable
from the database either — Policy_History.Notes only carries
descriptive text, and the real file lands in a flat /Uploads/ folder on
Handl's own web server with no accessible attachment table. So photos
are stored here, in our own Azure Blob Storage, and only a link to them
is written back into Handl (via HandlClient.add_report_note).

This app's own local disk can't be used either — confirmed via the
WebJobs investigation (see README/git history) that Azure deploys this
app as a per-instance temp extraction of the build artifact, not
persistent storage, so anything written to local disk is lost on
restart/redeploy and isn't shared across instances.

Until AZURE_STORAGE_CONNECTION_STRING is set, get_photo_storage()
returns MockPhotoStorage (writes under MEDIA_ROOT) so the rest of the
app can be built and tested without a real Storage Account.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from django.conf import settings


class PhotoStorage(ABC):
    @abstractmethod
    def upload(
        self, *, report_id: str, stage: str, filename: str, content: bytes, content_type: str
    ) -> str:
        """Stores one photo, returns its durable URL. `stage` (e.g.
        "before"/"after") and `report_id` namespace the blob path so
        two locksmiths' same-named phone photos never collide — unlike
        Handl's own flat /Uploads/ folder, which does exactly that."""


class MockPhotoStorage(PhotoStorage):
    """Local dev/tests: writes under MEDIA_ROOT via Django's own file
    storage instead of Azure, so the whole upload flow can be built and
    exercised without a real Storage Account."""

    def upload(self, *, report_id: str, stage: str, filename: str, content: bytes, content_type: str) -> str:
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        safe_name = Path(filename).name or "photo"
        path = f"job_photos/{report_id}/{stage}/{uuid.uuid4().hex}_{safe_name}"
        saved_path = default_storage.save(path, ContentFile(content))
        return default_storage.url(saved_path)


class AzureBlobPhotoStorage(PhotoStorage):
    """Real Azure Blob Storage-backed implementation. azure-storage-blob
    is imported lazily so importing this module (e.g. for MockPhotoStorage
    in dev/tests) doesn't require the package to be installed until it's
    actually needed."""

    def upload(self, *, report_id: str, stage: str, filename: str, content: bytes, content_type: str) -> str:
        from azure.storage.blob import BlobServiceClient, ContentSettings

        safe_name = Path(filename).name or "photo"
        blob_name = f"{report_id}/{stage}/{uuid.uuid4().hex}_{safe_name}"

        service = BlobServiceClient.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)
        container = service.get_container_client(settings.AZURE_STORAGE_CONTAINER)
        blob = container.get_blob_client(blob_name)
        blob.upload_blob(content, content_settings=ContentSettings(content_type=content_type))
        return blob.url


def get_photo_storage() -> PhotoStorage:
    if settings.AZURE_STORAGE_CONNECTION_STRING:
        return AzureBlobPhotoStorage()
    return MockPhotoStorage()
