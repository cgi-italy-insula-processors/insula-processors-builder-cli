"""Configuration, constants, and the workflow_dispatch contract.

The orchestrator workflow build-external.yml in the insula-processor-launcher repo
MUST declare exactly the inputs listed in WORKFLOW_INPUTS below and set a
`run-name:` that embeds ${{ inputs.correlation_id }} so this CLI can locate the run
it triggered.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

GITHUB_API = "https://api.github.com"

# The public LAUNCHER repo that hosts the orchestrator workflow. Users get write
# (or a fine-grained PAT with Actions: read/write) on THIS repo only, never on
# insula-processors-parent-pipeline. GitHub has no dispatch-only permission, so triggering needs
# write here; secrets are protected by an Environment branch rule (default branch
# only) and the launcher's branch protection, not by withholding write.
DEFAULT_PIPELINE_REPO = "cgi-italy/insula-processor-launcher"

# File name of the orchestrator workflow inside .github/workflows/ of the repo above.
DEFAULT_WORKFLOW = "build-external.yml"

# The orchestrator publishes the finalized CWL (published image already injected)
# as a GitHub Release on the launcher repo, tagged with this prefix + the run's
# correlation_id. The CLI looks the release up by that tag and reads the asset's
# public download URL, which it hands to Insula as the deploy reference (Insula
# fetches the CWL from that URL server-side).
RELEASE_TAG_PREFIX = "cwl-"

# workflow_dispatch input names the orchestrator must accept. Kept here so the
# CLI and the workflow cannot drift silently.
WORKFLOW_INPUTS = (
    "repo_url",
    "ref",
    "correlation_id",
    "bypass_gate",
)

# Environment variable names for secrets. Never echoed.
ENV_API_TOKEN = "INSULA_API_TOKEN"
ENV_GITHUB_TOKEN = "INSULA_GITHUB_TOKEN"

# GitHub App client id used by `insula-processors-builder login` (OAuth device flow).
# NON-SECRET: safe to commit. Shipped as the default so `login` works with no
# config. Override with INSULA_GITHUB_APP_CLIENT_ID or app_client_id in config.
DEFAULT_APP_CLIENT_ID = "Iv23liZgFmrhfIJzJDUS"
ENV_APP_CLIENT_ID = "INSULA_GITHUB_APP_CLIENT_ID"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"


@dataclass
class Settings:
    """Non-secret settings. There is NO settings config file: every value below is
    a built-in default overridable by a command-line flag. The only thing stored on
    disk is the api token (see api_token_path), so users never hand-edit config."""

    pipeline_repo: str = DEFAULT_PIPELINE_REPO
    workflow: str = DEFAULT_WORKFLOW
    # Branch of the PIPELINE repo whose workflow runs. This is NOT the user's ref
    # (that travels as a workflow input). workflow_dispatch's top-level ref selects
    # which branch of insula-processor-launcher executes (contract 2: it must be
    # the launcher's default branch; the launcher then calls parent-pipeline at
    # its own pinned SHA).
    pipeline_ref: str = "main"
    # OGC API - Processes deploy endpoint (Part 2 DRU). Default set for Insula.
    publish_endpoint: Optional[str] = "https://insula.earth/ogcapi/processes"
    # The deploy request is fixed: a POST of an ogcapppkg JSON document referencing
    # the CWL by URL (executionUnit.href), with header Authorization: Apikey <token>
    # and Content-Type application/ogcapppkg+json. Neither is configurable (see
    # publish.publish_cwl / publish._CONTENT_TYPE).
    # Verify the TLS certificate of the publish endpoint. Disable (--insecure) when a
    # corporate TLS-inspecting proxy or an internal CA re-signs the connection with a
    # certificate the CLI cannot verify.
    verify_tls: bool = True
    # Overridable with --poll-timeout / --poll-interval on `create`.
    poll_timeout_seconds: int = 1800
    poll_interval_seconds: int = 10
    # GitHub App client id for `login` (device flow); empty = not configured.
    app_client_id: str = DEFAULT_APP_CLIENT_ID


def _config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "insula-processors-builder")


def token_cache_path() -> str:
    """Where `login` stores the device-flow access token (mode 0600)."""
    return os.path.join(_config_dir(), "token")


def api_token_path() -> str:
    """Where `set-api-token` stores the Insula api token (mode 0600). A plain
    single-line file, not TOML: the token holds characters a shell (and a TOML
    string) would mangle, and users must never hand-edit config."""
    return os.path.join(_config_dir(), "api-token")
