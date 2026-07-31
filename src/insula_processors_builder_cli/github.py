"""Minimal GitHub REST client for triggering the orchestrator and locating its CWL.

Capabilities used, all backed by an Actions:write fine-grained PAT:
  - trigger workflow_dispatch
  - read workflow runs
  - read the finalized-CWL release (public read) to get its download URL
The token needs Actions: write to dispatch; it needs NO Contents permission, so it
cannot push code or alter workflows (reading a public repo's release needs no scope).
"""

from __future__ import annotations

import time
from typing import Dict, Optional

import requests

from .config import GITHUB_API, RELEASE_TAG_PREFIX
from .errors import ArtifactError, DispatchError, RunFailedError

_API_VERSION = "2022-11-28"

# Statuses worth retrying rather than aborting a long poll. 403 is included ONLY
# when it looks like a rate limit (see _is_rate_limited); a genuine permission
# 403 is surfaced immediately with its body.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_GET_ATTEMPTS = 6
_MAX_RETRY_DELAY = 120


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

    @staticmethod
    def _is_rate_limited(resp: requests.Response) -> bool:
        """A 403/429 that carries rate-limit signals (rather than a real deny)."""
        if resp.status_code not in (403, 429):
            return False
        if resp.headers.get("Retry-After"):
            return True
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            return True
        body = resp.text.lower()
        return "rate limit" in body or "secondary rate" in body

    @staticmethod
    def _retry_delay(resp: requests.Response, attempt: int) -> int:
        """How long to wait before the next attempt: honor Retry-After, then the
        rate-limit reset, else exponential backoff. Capped so a poll cannot hang."""
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return min(_MAX_RETRY_DELAY, int(retry_after))
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset and reset.isdigit():
            return max(1, min(_MAX_RETRY_DELAY, int(reset) - int(time.time())))
        return min(60, 2 ** attempt)

    def _get(
        self,
        path: str,
        params: Optional[Dict[str, str]] = None,
        *,
        timeout: int = 30,
        deadline: Optional[float] = None,
    ) -> requests.Response:
        """GET with retry on transient failures (rate limits, 5xx, network errors).

        Returns a 200 Response. Retries transient statuses honoring rate-limit
        headers; raises RunFailedError (with the response body) on a non-retryable
        status such as 401/404 or a genuine 403, or after exhausting retries. A
        single blip no longer aborts a long-running poll.

        deadline (time.monotonic seconds) caps the retrying so this call cannot
        overrun the caller's own budget - e.g. find_run's short appearance window,
        which the internal 6-attempt/120s backoff would otherwise blow past.
        """
        last_detail = ""
        for attempt in range(_MAX_GET_ATTEMPTS):
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                resp = self._session.get(self._url(path), params=params, timeout=timeout)
            except requests.RequestException as exc:
                last_detail = str(exc)
                delay = min(60, 2 ** attempt)
            else:
                if resp.status_code == 200:
                    return resp
                if resp.status_code in _RETRYABLE_STATUS or self._is_rate_limited(resp):
                    last_detail = f"{resp.status_code}: {resp.text.strip()[:200]}"
                    delay = self._retry_delay(resp, attempt)
                else:
                    raise RunFailedError(
                        f"GitHub API {resp.status_code} for {path}: {resp.text.strip()[:300]}"
                    )
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                delay = min(delay, remaining)  # never sleep past the caller's deadline
            time.sleep(delay)
        raise RunFailedError(f"GitHub API kept failing for {path}: {last_detail}")

    def dispatch(self, workflow: str, ref: str, inputs: Dict[str, str]) -> None:
        """Trigger workflow_dispatch, retrying transient failures like _get so a
        single 5xx/rate-limit/network blip on the entry point does not abort."""
        path = f"/actions/workflows/{workflow}/dispatches"
        last_detail = ""
        for attempt in range(_MAX_GET_ATTEMPTS):
            try:
                resp = self._session.post(
                    self._url(path), json={"ref": ref, "inputs": inputs}, timeout=30
                )
            except requests.RequestException as exc:
                last_detail = str(exc)
                time.sleep(min(60, 2 ** attempt))
                continue
            if resp.status_code == 204:
                return
            if resp.status_code in _RETRYABLE_STATUS or self._is_rate_limited(resp):
                last_detail = f"{resp.status_code}: {resp.text.strip()[:200]}"
                time.sleep(self._retry_delay(resp, attempt))
                continue
            hint = ""
            if resp.status_code == 404:
                # The single most common first-run failure: login works for any
                # GitHub account, but dispatch 404s until a maintainer onboards you.
                hint = (
                    " - the workflow/repo was not found, or your account is not yet"
                    " onboarded on the launcher (a maintainer must grant you access);"
                    " confirm access before retrying"
                )
            raise DispatchError(
                f"dispatch failed ({resp.status_code}){hint}: {resp.text.strip()[:300]}"
            )
        raise DispatchError(f"dispatch kept failing for {workflow}: {last_detail}")

    def find_run(
        self, workflow: str, correlation_id: str, appear_timeout: int = 180
    ) -> int:
        """Poll for the run this dispatch created, matched by correlation id in the
        run name. workflow_dispatch returns no run id, so matching is the only way.
        Scans the 100 most-recent dispatch runs over a 180s window so a run queued
        behind a busy runner pool is not missed."""
        deadline = time.monotonic() + appear_timeout
        params = {"event": "workflow_dispatch", "per_page": "100"}
        while time.monotonic() < deadline:
            resp = self._get(
                f"/actions/workflows/{workflow}/runs", params=params, deadline=deadline
            )
            for run in resp.json().get("workflow_runs", []):
                haystack = f"{run.get('name', '')} {run.get('display_title', '')}"
                if correlation_id in haystack:
                    return int(run["id"])
            time.sleep(3)
        raise DispatchError(
            "could not locate the dispatched run; it may not have started"
        )

    def wait_for_run(
        self, run_id: int, timeout: int, interval: int, *, allow_failure: bool = False
    ) -> str:
        """Block until the run completes. Returns its conclusion (e.g. 'success').

        Transient API failures (rate limits, 5xx) are retried inside _get and do
        not end the wait; only the overall timeout or a completed non-success run
        stops it. With allow_failure=True a non-success conclusion is returned
        rather than raised (the --bypass flow: the run concludes 'failure' because
        a scan failed, yet the publish job ran and produced the CWL artifact).
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self._get(f"/actions/runs/{run_id}", deadline=deadline)
            run = resp.json()
            if run.get("status") == "completed":
                conclusion = run.get("conclusion") or "unknown"
                if conclusion != "success" and not allow_failure:
                    stages = self._failed_job_names(run_id)
                    detail = f" at stage(s): {stages}" if stages else ""
                    raise RunFailedError(
                        f"pipeline run concluded '{conclusion}'{detail}: {run.get('html_url')}"
                    )
                return conclusion
            time.sleep(interval)
        raise RunFailedError(
            f"pipeline run did not finish within {timeout}s; still running at "
            f"{self.run_url(run_id)} (raise poll_timeout_seconds if builds are slow)"
        )

    def run_url(self, run_id: int) -> str:
        return f"https://github.com/{self._repo}/actions/runs/{run_id}"

    def _failed_job_names(self, run_id: int) -> str:
        """Best-effort: names of the run's jobs that did not succeed, so a failure
        can name the stage (e.g. 'security') instead of only a run URL to dig through.
        Returns '' if the jobs cannot be read (never masks the underlying failure)."""
        try:
            resp = self._get(f"/actions/runs/{run_id}/jobs")
        except RunFailedError:
            return ""
        names = [
            j.get("name", "?")
            for j in resp.json().get("jobs", [])
            if j.get("conclusion") not in (None, "success", "skipped")
        ]
        return ", ".join(names)

    def get_cwl_url(self, correlation_id: str, appear_timeout: int = 60) -> str:
        """Return the public download URL of the finalized CWL the run published.

        The finalize step creates a Release tagged RELEASE_TAG_PREFIX+correlation_id
        with the CWL as its single asset. Its browser_download_url is what the CLI
        hands to Insula (executionUnit.href); Insula fetches the CWL from there.

        The release is created at the very end of the run and the GitHub API is
        eventually consistent, so a 404 right after the run completes is a
        read-after-write lag, not a real absence: poll over a short window before
        giving up. All failure modes raise ArtifactError (a missing artifact), never
        RunFailedError.
        """
        tag = f"{RELEASE_TAG_PREFIX}{correlation_id}"
        deadline = time.monotonic() + appear_timeout
        last = ""
        while True:
            try:
                resp = self._session.get(self._url(f"/releases/tags/{tag}"), timeout=30)
            except requests.RequestException as exc:
                last = str(exc)
            else:
                if resp.status_code == 200:
                    assets = resp.json().get("assets", [])
                    url = assets[0].get("browser_download_url") if assets else None
                    if not url:
                        raise ArtifactError(
                            f"the run's CWL release '{tag}' has no downloadable asset"
                        )
                    return url
                if resp.status_code == 404:
                    last = "404 (release not yet visible)"
                elif resp.status_code in _RETRYABLE_STATUS or self._is_rate_limited(resp):
                    last = f"{resp.status_code}: {resp.text.strip()[:200]}"
                else:
                    raise ArtifactError(
                        f"could not read CWL release '{tag}' "
                        f"({resp.status_code}): {resp.text.strip()[:200]}"
                    )
            if time.monotonic() >= deadline:
                raise ArtifactError(
                    f"CWL release '{tag}' did not appear within {appear_timeout}s: {last}"
                )
            time.sleep(3)
