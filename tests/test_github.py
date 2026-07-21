"""Unit tests for the GitHub client (no network; _get / session are stubbed)."""

from __future__ import annotations

import io
import zipfile

import pytest

from insula_processors_builder_cli import github
from insula_processors_builder_cli.errors import ArtifactError, DispatchError


class FakeResp:
    def __init__(self, status_code=200, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.content = content
        self.text = text
        self.headers = {}

    def json(self):
        return self._json


def _zip_bytes(name, data):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(name, data)
    return buf.getvalue()


def _client():
    return github.GitHubClient("token", "owner/repo")


def test_download_artifact_returns_basename(monkeypatch):
    client = _client()
    zip_bytes = _zip_bytes("nested/dir/proc.cwl", b"cwlVersion: v1.2\n")

    def fake_get(path, params=None, *, timeout=30):
        if path.endswith("/artifacts"):
            return FakeResp(json_data={"artifacts": [{"name": "cwl", "id": 7}]})
        return FakeResp(content=zip_bytes)

    monkeypatch.setattr(client, "_get", fake_get)
    name, data = client.download_artifact(1, "cwl")
    assert name == "proc.cwl"  # basename only, no path traversal
    assert data == b"cwlVersion: v1.2\n"


def test_download_artifact_rejects_oversize(monkeypatch):
    client = _client()
    big = b"x" * (github._MAX_ARTIFACT_BYTES + 10)
    zip_bytes = _zip_bytes("proc.cwl", big)

    def fake_get(path, params=None, *, timeout=30):
        if path.endswith("/artifacts"):
            return FakeResp(json_data={"artifacts": [{"name": "cwl", "id": 7}]})
        return FakeResp(content=zip_bytes)

    monkeypatch.setattr(client, "_get", fake_get)
    with pytest.raises(ArtifactError):
        client.download_artifact(1, "cwl")


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
