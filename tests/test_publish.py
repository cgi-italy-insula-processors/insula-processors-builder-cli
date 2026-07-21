"""Retry/backoff behavior of the local CWL deploy POST (no network)."""

from __future__ import annotations

import pytest
import requests

from insula_processors_builder_cli import publish
from insula_processors_builder_cli.config import Settings
from insula_processors_builder_cli.errors import PublishError


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def _post_sequence(monkeypatch, outcomes):
    """Stub requests.post to yield each outcome in turn (exception or _Resp)."""
    calls = []

    def fake_post(url, **kwargs):
        outcome = outcomes[len(calls)]
        calls.append(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(publish.requests, "post", fake_post)
    monkeypatch.setattr(publish.time, "sleep", lambda _s: None)
    return calls


def _publish() -> str:
    return publish.publish_cwl(Settings(), "tok", b"cwl", "p.cwl")


def test_retries_connection_error_then_succeeds(monkeypatch):
    calls = _post_sequence(
        monkeypatch, [requests.exceptions.ConnectionError("refused"), _Resp(200, "ok")]
    )
    assert _publish() == "ok"
    assert len(calls) == 2


def test_retries_transient_status_then_succeeds(monkeypatch):
    calls = _post_sequence(monkeypatch, [_Resp(503, "busy"), _Resp(201, "created")])
    assert _publish() == "created"
    assert len(calls) == 2


def test_gives_up_after_bounded_attempts(monkeypatch):
    calls = _post_sequence(
        monkeypatch,
        [requests.exceptions.ConnectionError("refused")] * publish._RETRY_ATTEMPTS,
    )
    with pytest.raises(PublishError):
        _publish()
    assert len(calls) == publish._RETRY_ATTEMPTS


def test_409_surfaces_already_deployed_without_retry(monkeypatch):
    calls = _post_sequence(monkeypatch, [_Resp(409, "conflict")])
    with pytest.raises(PublishError, match="already deployed"):
        _publish()
    assert len(calls) == 1


def test_4xx_is_not_retried(monkeypatch):
    calls = _post_sequence(monkeypatch, [_Resp(400, "bad request")])
    with pytest.raises(PublishError):
        _publish()
    assert len(calls) == 1


def test_read_timeout_is_not_retried(monkeypatch):
    # A read timeout means the server may already be processing the deploy;
    # a blind retry could deploy twice, so it must fail immediately.
    calls = _post_sequence(monkeypatch, [requests.exceptions.ReadTimeout("slow")])
    with pytest.raises(PublishError, match="may still have been processed"):
        _publish()
    assert len(calls) == 1


def test_500_is_not_retried(monkeypatch):
    # A plain 500 may mean the server is processing the deploy; retrying could
    # deploy twice, so it must stay OUT of _RETRY_STATUS.
    calls = _post_sequence(monkeypatch, [_Resp(500, "boom")])
    with pytest.raises(PublishError):
        _publish()
    assert len(calls) == 1


def test_504_is_not_retried(monkeypatch):
    # 504 = the gateway forwarded the request and the backend may still process
    # it: the proxy-side twin of ReadTimeout, so it must fail immediately.
    calls = _post_sequence(monkeypatch, [_Resp(504, "gateway timeout")])
    with pytest.raises(PublishError, match="may still have been processed"):
        _publish()
    assert len(calls) == 1
