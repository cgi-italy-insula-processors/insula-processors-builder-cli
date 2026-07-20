"""GitHub App OAuth device flow, so users authenticate without creating a PAT.

`insula-processor login` runs the device flow and caches the resulting
user-to-server token locally (mode 0600). `create` uses it when
INSULA_GITHUB_TOKEN is not set. The token is bounded by the user's access to the
launcher repo, exactly like a PAT would be.
"""

from __future__ import annotations

import os
import time

import requests

from . import config
from .errors import CliError

_JSON = {"Accept": "application/json"}


def device_login(client_id: str) -> str:
    """Run the device flow and return an access token. Prints the user code."""
    if not client_id:
        raise CliError(
            "device login is not configured: set app_client_id in config or "
            f"{config.ENV_APP_CLIENT_ID}, or use a PAT via {config.ENV_GITHUB_TOKEN}"
        )

    resp = requests.post(
        config.DEVICE_CODE_URL, headers=_JSON, data={"client_id": client_id}, timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    if "device_code" not in data:
        raise CliError(f"device code request failed: {data.get('error', 'unknown')}")

    print(f"Open {data['verification_uri']} and enter code: {data['user_code']}")

    interval = int(data.get("interval", 5))
    deadline = time.monotonic() + int(data.get("expires_in", 900))
    while time.monotonic() < deadline:
        time.sleep(interval)
        poll = requests.post(
            config.ACCESS_TOKEN_URL,
            headers=_JSON,
            data={
                "client_id": client_id,
                "device_code": data["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=30,
        ).json()
        if "access_token" in poll:
            return poll["access_token"]
        error = poll.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = int(poll.get("interval", interval + 5))
            continue
        raise CliError(f"login failed: {error or 'unknown error'}")
    raise CliError("login timed out; run `insula-processor login` again")


def save_token(token: str) -> None:
    path = config.token_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Create with 0600 before writing so the token is never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)


def load_cached_token() -> str:
    path = config.token_cache_path()
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return ""


def clear_token() -> None:
    try:
        os.remove(config.token_cache_path())
    except FileNotFoundError:
        pass
