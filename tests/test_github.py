"""Unit tests for the GitHub client (no network; _get / session are stubbed)."""

from __future__ import annotations

import pytest

from insula_processors_builder_cli import github
from insula_processors_builder_cli.errors import ArtifactError, DispatchError, RunFailedError


class FakeResp:
    def __init__(self, status_code=200, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.content = content
        self.text = text
        self.headers = {}

    def json(self):
        return self._json


def _client():
    return github.GitHubClient("token", "owner/repo")


def test_get_cwl_url_returns_asset_download_url(monkeypatch):
    client = _client()
    captured = {}

    def fake_get(url, timeout=30):
        captured["url"] = url
        return FakeResp(status_code=200, json_data={
            "assets": [{"browser_download_url": "https://example/download/abc/app.cwl"}]
        })

    monkeypatch.setattr(client._session, "get", fake_get)
    url = client.get_cwl_url("abc123")
    assert url == "https://example/download/abc/app.cwl"
    # Looked up by the RELEASE_TAG_PREFIX + correlation_id tag.
    assert captured["url"].endswith("/releases/tags/cwl-abc123")


def test_get_cwl_url_raises_when_no_asset(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client._session, "get",
        lambda url, timeout=30: FakeResp(status_code=200, json_data={"assets": []}),
    )
    with pytest.raises(ArtifactError):
        client.get_cwl_url("abc123")


def test_get_cwl_url_retries_404_then_succeeds(monkeypatch):
    # A 404 right after the run completes is a read-after-write lag, not absence.
    client = _client()
    monkeypatch.setattr(github.time, "sleep", lambda *_: None)
    monkeypatch.setattr(github.time, "monotonic", lambda: 0.0)  # never hit the deadline
    seq = [
        FakeResp(status_code=404, text="not found"),
        FakeResp(status_code=200, json_data={
            "assets": [{"browser_download_url": "https://example/app.cwl"}]
        }),
    ]
    calls = {"n": 0}

    def fake_get(url, timeout=30):
        r = seq[calls["n"]]
        calls["n"] += 1
        return r

    monkeypatch.setattr(client._session, "get", fake_get)
    assert client.get_cwl_url("abc123", appear_timeout=30) == "https://example/app.cwl"
    assert calls["n"] == 2


def test_get_cwl_url_permanent_status_raises_artifact_error(monkeypatch):
    # A non-retryable status (e.g. 401) surfaces as ArtifactError, never RunFailedError.
    client = _client()
    monkeypatch.setattr(
        client._session, "get",
        lambda url, timeout=30: FakeResp(status_code=401, text="unauthorized"),
    )
    with pytest.raises(ArtifactError):
        client.get_cwl_url("abc123")


def test_find_run_matches_correlation(monkeypatch):
    client = _client()
    runs = {"workflow_runs": [{"id": 42, "name": "build x [abc123]", "display_title": ""}]}
    monkeypatch.setattr(client, "_get", lambda *a, **k: FakeResp(json_data=runs))
    assert client.find_run("build-external.yml", "abc123") == 42


def test_find_run_not_found(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_get", lambda *a, **k: FakeResp(json_data={"workflow_runs": []}))
    with pytest.raises(DispatchError):
        client.find_run("build-external.yml", "missing", appear_timeout=0)


def test_dispatch_retries_then_succeeds(monkeypatch):
    client = _client()
    monkeypatch.setattr(github.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_post(url, json=None, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(status_code=502, text="bad gateway")
        return FakeResp(status_code=204)

    monkeypatch.setattr(client._session, "post", fake_post)
    client.dispatch("build-external.yml", "main", {"a": "b"})
    assert calls["n"] == 2


def test_dispatch_raises_on_permanent_error(monkeypatch):
    client = _client()
    monkeypatch.setattr(github.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        client._session, "post", lambda *a, **k: FakeResp(status_code=404, text="no workflow")
    )
    with pytest.raises(DispatchError):
        client.dispatch("build-external.yml", "main", {"a": "b"})


def test_dispatch_404_hints_onboarding(monkeypatch):
    client = _client()
    monkeypatch.setattr(github.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        client._session, "post", lambda *a, **k: FakeResp(status_code=404, text="Not Found")
    )
    with pytest.raises(DispatchError, match="onboarded"):
        client.dispatch("build-external.yml", "main", {"a": "b"})


def test_get_retries_5xx_then_succeeds(monkeypatch):
    client = _client()
    monkeypatch.setattr(github.time, "sleep", lambda *_: None)
    seq = [FakeResp(status_code=502, text="bad"), FakeResp(status_code=200, json_data={"ok": 1})]
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=30):
        r = seq[calls["n"]]
        calls["n"] += 1
        return r

    monkeypatch.setattr(client._session, "get", fake_get)
    assert client._get("/x").status_code == 200
    assert calls["n"] == 2


def test_get_raises_on_non_retryable_status(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client._session, "get", lambda url, params=None, timeout=30: FakeResp(status_code=404, text="nf")
    )
    with pytest.raises(RunFailedError):
        client._get("/x")


def test_wait_for_run_returns_success(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client, "_get", lambda *a, **k: FakeResp(json_data={"status": "completed", "conclusion": "success"})
    )
    assert client.wait_for_run(1, timeout=5, interval=0) == "success"


def test_wait_for_run_failure_names_stage(monkeypatch):
    client = _client()

    def fake_get(path, params=None, *, timeout=30, deadline=None):
        if path.endswith("/jobs"):
            return FakeResp(json_data={"jobs": [
                {"name": "security", "conclusion": "failure"},
                {"name": "setup", "conclusion": "success"},
            ]})
        return FakeResp(json_data={"status": "completed", "conclusion": "failure", "html_url": "u"})

    monkeypatch.setattr(client, "_get", fake_get)
    with pytest.raises(RunFailedError, match="security"):
        client.wait_for_run(1, timeout=5, interval=0)


def test_wait_for_run_allow_failure_returns_conclusion(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client, "_get",
        lambda *a, **k: FakeResp(json_data={"status": "completed", "conclusion": "failure", "html_url": "u"}),
    )
    assert client.wait_for_run(1, timeout=5, interval=0, allow_failure=True) == "failure"
