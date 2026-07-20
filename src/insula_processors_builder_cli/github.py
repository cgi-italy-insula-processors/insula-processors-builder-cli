"""Minimal GitHub REST client for triggering the orchestrator and collecting its CWL.

Only three capabilities are used, all backed by an Actions:write fine-grained PAT:
  - trigger workflow_dispatch
  - read workflow runs / artifacts
  - download an artifact
The token needs Actions: write to dispatch; it needs NO Contents permission, so it
cannot push code or alter workflows.
"""

from __future__ import annotations

import io
import time
import zipfile
from typing import Dict, Tuple

import requests

from .config import GITHUB_API
from .errors import ArtifactError, DispatchError, RunFailedError

_API_VERSION = "2022-11-28"


class GitHubClient:
    def __init__(self, token: str, repo: str, api_base: str = GITHUB_API):
        if "/" not in repo:
            raise DispatchError(f"pipeline repo must be 'owner/name', got '{repo}'")
        self._repo = repo
        self._api = api_base.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION,
            }
        )

    def _url(self, path: str) -> str:
        return f"{self._api}/repos/{self._repo}{path}"

    def dispatch(self, workflow: str, ref: str, inputs: Dict[str, str]) -> None:
        resp = self._session.post(
            self._url(f"/actions/workflows/{workflow}/dispatches"),
            json={"ref": ref, "inputs": inputs},
            timeout=30,
        )
        if resp.status_code != 204:
            raise DispatchError(
                f"dispatch failed ({resp.status_code}): {resp.text.strip()[:300]}"
            )

    def find_run(
        self, workflow: str, correlation_id: str, appear_timeout: int = 60
    ) -> int:
        """Poll for the run this dispatch created, matched by correlation id in the
        run name. workflow_dispatch returns no run id, so matching is the only way."""
        deadline = time.monotonic() + appear_timeout
        params = {"event": "workflow_dispatch", "per_page": "30"}
        while time.monotonic() < deadline:
            resp = self._session.get(
                self._url(f"/actions/workflows/{workflow}/runs"),
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            for run in resp.json().get("workflow_runs", []):
                haystack = f"{run.get('name', '')} {run.get('display_title', '')}"
                if correlation_id in haystack:
                    return int(run["id"])
            time.sleep(3)
        raise DispatchError(
            "could not locate the dispatched run; it may not have started"
        )

    def wait_for_run(self, run_id: int, timeout: int, interval: int) -> str:
        """Block until the run completes. Returns its conclusion (e.g. 'success')."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self._session.get(self._url(f"/actions/runs/{run_id}"), timeout=30)
            resp.raise_for_status()
            run = resp.json()
            if run.get("status") == "completed":
                conclusion = run.get("conclusion") or "unknown"
                if conclusion != "success":
                    raise RunFailedError(
                        f"pipeline run concluded '{conclusion}': {run.get('html_url')}"
                    )
                return conclusion
            time.sleep(interval)
        raise RunFailedError(f"pipeline run did not finish within {timeout}s")

    def run_url(self, run_id: int) -> str:
        return f"https://github.com/{self._repo}/actions/runs/{run_id}"

    def download_artifact(self, run_id: int, name: str) -> Tuple[str, bytes]:
        """Return (filename, bytes) of the single file inside the named artifact zip."""
        resp = self._session.get(
            self._url(f"/actions/runs/{run_id}/artifacts"), timeout=30
        )
        resp.raise_for_status()
        artifact = next(
            (a for a in resp.json().get("artifacts", []) if a.get("name") == name),
            None,
        )
        if artifact is None:
            raise ArtifactError(f"artifact '{name}' not found on the run")

        dl = self._session.get(
            self._url(f"/actions/artifacts/{artifact['id']}/zip"),
            timeout=120,
        )
        dl.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(dl.content)) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            if not names:
                raise ArtifactError(f"artifact '{name}' is empty")
            with archive.open(names[0]) as member:
                return names[0], member.read()
