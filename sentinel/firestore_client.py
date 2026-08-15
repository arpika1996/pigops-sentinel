"""One shared Firestore client.

- Against the real project it authenticates with Application Default
  Credentials (``gcloud auth application-default login`` locally, the
  Cloud Run service account in production). No key files, ever.
- If ``FIRESTORE_EMULATOR_HOST`` is set, the client library talks to the
  local emulator with anonymous credentials automatically.
"""

from __future__ import annotations

from functools import lru_cache

from google.cloud import firestore

from .config import settings


@lru_cache(maxsize=1)
def get_db() -> firestore.Client:
    """Return the process-wide Firestore client (created lazily, once)."""
    return firestore.Client(project=settings.google_cloud_project)


def describe_target() -> str:
    """Human-readable description of where writes will land — used in logs."""
    if settings.uses_emulator:
        return f"Firestore EMULATOR at {settings.firestore_emulator_host} (project {settings.google_cloud_project})"
    return f"Firestore project {settings.google_cloud_project}"
