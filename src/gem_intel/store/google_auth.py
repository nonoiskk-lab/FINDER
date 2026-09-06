"""Google Workspace credentials.

Two supported modes, in priority order:

1. **Service account** (``GOOGLE_SERVICE_ACCOUNT_JSON`` or
   ``GOOGLE_APPLICATION_CREDENTIALS``) — the right choice for an unattended
   daily run. Share the target Drive folder with the service account's email,
   or use a shared drive.
2. **OAuth user credentials** (``GOOGLE_OAUTH_TOKEN_JSON``) — for a personal
   Drive where a service account cannot be granted access.

Every Google integration degrades gracefully: if credentials are missing, the
run still completes and writes the report to disk, and the manifest records
``degraded_modes: ["google_unavailable"]`` so the operator can see why no
link appeared.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gem_intel.observability import get_logger

log = get_logger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]


class GoogleUnavailable(RuntimeError):
    """Raised when Google APIs cannot be used. Always caught by the pipeline."""


@dataclass
class GoogleClients:
    drive: Any
    docs: Any
    sheets: Any
    principal: str = ""


def load_credentials() -> Any:
    try:
        from google.oauth2 import service_account
        from google.oauth2.credentials import Credentials as UserCredentials
    except ImportError as exc:
        raise GoogleUnavailable(
            "google-auth is not installed — run `pip install -r requirements.txt`"
        ) from exc

    inline = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if inline:
        try:
            info = json.loads(inline)
        except json.JSONDecodeError as exc:
            raise GoogleUnavailable(
                "GOOGLE_SERVICE_ACCOUNT_JSON is set but is not valid JSON"
            ) from exc
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    key_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if key_file:
        path = Path(key_file)
        if not path.exists():
            raise GoogleUnavailable(f"GOOGLE_APPLICATION_CREDENTIALS points at {path}, "
                                    "which does not exist")
        return service_account.Credentials.from_service_account_file(str(path), scopes=SCOPES)

    token_json = os.getenv("GOOGLE_OAUTH_TOKEN_JSON", "").strip()
    if token_json:
        try:
            info = json.loads(token_json)
        except json.JSONDecodeError as exc:
            raise GoogleUnavailable(
                "GOOGLE_OAUTH_TOKEN_JSON is set but is not valid JSON"
            ) from exc
        return UserCredentials.from_authorized_user_info(info, scopes=SCOPES)

    raise GoogleUnavailable(
        "No Google credentials found. Set GOOGLE_SERVICE_ACCOUNT_JSON, "
        "GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_OAUTH_TOKEN_JSON "
        "(see docs/GOOGLE_SETUP.md)."
    )


def build_clients(credentials: Any = None) -> GoogleClients:
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GoogleUnavailable(
            "google-api-python-client is not installed — "
            "run `pip install -r requirements.txt`"
        ) from exc

    creds = credentials or load_credentials()
    principal = getattr(creds, "service_account_email", "") or "oauth-user"
    # cache_discovery=False avoids a noisy warning and a filesystem write in CI.
    return GoogleClients(
        drive=build("drive", "v3", credentials=creds, cache_discovery=False),
        docs=build("docs", "v1", credentials=creds, cache_discovery=False),
        sheets=build("sheets", "v4", credentials=creds, cache_discovery=False),
        principal=principal,
    )


def try_build_clients() -> GoogleClients | None:
    """Build clients, or return ``None`` after logging why not."""
    try:
        clients = build_clients()
    except GoogleUnavailable as exc:
        log.warning("Google Workspace integration unavailable", reason=str(exc))
        return None
    except Exception as exc:                      # noqa: BLE001
        log.warning("Google Workspace integration failed to initialise",
                    error=f"{type(exc).__name__}: {exc}")
        return None
    log.info("Google Workspace ready", principal=clients.principal)
    return clients
