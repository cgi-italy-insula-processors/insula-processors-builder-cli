"""POST the built CWL to the consuming endpoint, authenticated with the user's
api token. This runs locally: the api token never reaches GitHub Actions."""

from __future__ import annotations

import requests

from .config import Settings
from .errors import PublishError


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

    try:
        resp = requests.post(
            settings.publish_endpoint, headers=headers, timeout=60, **kwargs
        )
    except requests.RequestException as exc:
        # str(exc) can include the URL but never the token (it is only in headers).
        raise PublishError(f"publish request failed: {exc}") from exc

    if resp.status_code >= 400:
        raise PublishError(
            f"endpoint rejected the CWL ({resp.status_code}): {resp.text.strip()[:300]}"
        )
    return resp.text.strip()
