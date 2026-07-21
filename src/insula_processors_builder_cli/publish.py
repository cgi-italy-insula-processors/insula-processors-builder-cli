"""POST the built CWL to the consuming endpoint, authenticated with the user's
api token. This runs locally: the api token never reaches GitHub Actions."""

from __future__ import annotations

import time
from typing import Optional

import requests

from .config import Settings
from .errors import PublishError

# Bounded retry for transient deploy failures. The POST is NOT idempotent, so
# retry ONLY when the request cannot have been processed: connection errors
# (refused/reset/DNS, incl. connect timeout) and 429/502/503, where the request
# was rejected before reaching the application. NOT retried: a read timeout or a
# 504 (the gateway FORWARDED the request and the backend may still be processing
# it) or a plain 500 - retrying those could deploy twice, so they surface as
# errors instead.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 2
_RETRY_STATUS = (429, 502, 503)


def publish_cwl(settings: Settings, api_token: str, cwl_bytes: bytes, filename: str) -> str:
    """Deploy the CWL to the OGC API - Processes endpoint. Returns the response text."""
    if not settings.publish_endpoint:
        raise PublishError(
            "no publish endpoint configured (set publish_endpoint in config or "
            "pass --endpoint)"
        )

    headers = {settings.auth_header: settings.auth_format.format(token=api_token)}
    if settings.upload_mode == "multipart":
        kwargs = {"files": {settings.upload_field: (filename, cwl_bytes, settings.content_type)}}
    else:
        # OGC API - Processes deploy: raw application package as the request body.
        headers["Content-Type"] = settings.content_type
        kwargs = {"data": cwl_bytes}

    last_error: Optional[Exception] = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(_RETRY_BACKOFF_SECONDS * (attempt - 1))
        try:
            resp = requests.post(
                settings.publish_endpoint, headers=headers, timeout=60, **kwargs
            )
        except requests.exceptions.ReadTimeout as exc:
            # The endpoint received the request and may still be processing it;
            # a blind retry could deploy the process twice.
            raise PublishError(
                f"publish request timed out waiting for the response: {exc}; the "
                "deploy may still have been processed - check the platform before retrying"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            # The request never reached the endpoint; a retry cannot double-deploy.
            last_error = exc
            continue
        except requests.RequestException as exc:
            # str(exc) can include the URL but never the token (it is only in headers).
            raise PublishError(f"publish request failed: {exc}") from exc

        if resp.status_code == 409:
            raise PublishError(
                f"endpoint reports the process as already deployed (409): "
                f"{resp.text.strip()[:300]}"
            )
        if resp.status_code == 504:
            # The gateway forwarded the request and timed out waiting for the
            # backend; the deploy may still complete - same class as ReadTimeout.
            raise PublishError(
                "endpoint gateway timed out (504); the deploy may still have been "
                "processed - check the platform before retrying"
            )
        if resp.status_code in _RETRY_STATUS:
            last_error = PublishError(
                f"endpoint returned a transient {resp.status_code}: {resp.text.strip()[:300]}"
            )
            continue
        if resp.status_code >= 400:
            raise PublishError(
                f"endpoint rejected the CWL ({resp.status_code}): {resp.text.strip()[:300]}"
            )
        return resp.text.strip()

    raise PublishError(f"publish failed after {_RETRY_ATTEMPTS} attempts: {last_error}")
