"""Token-cache and device-flow tests (no network; requests/time are stubbed)."""

from __future__ import annotations

import os

import pytest

from insula_processors_builder_cli import auth, config
from insula_processors_builder_cli.errors import CliError


def test_save_load_clear_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    auth.save_token("tok-123")
    assert auth.load_cached_token() == "tok-123"
    auth.clear_token()
    assert auth.load_cached_token() == ""


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_save_token_is_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    auth.save_token("tok")
    assert os.stat(config.token_cache_path()).st_mode & 0o777 == 0o600


def test_api_token_save_load_clear_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    auth.save_api_token("api-tok")
    assert auth.load_api_token() == "api-tok"
    auth.clear_api_token()
    assert auth.load_api_token() == ""


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_save_api_token_is_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    auth.save_api_token("api-tok")
    assert os.stat(config.api_token_path()).st_mode & 0o777 == 0o600


def test_api_token_file_is_separate_from_login_token(tmp_path, monkeypatch):
    # The Insula api token must never be mistaken for the GitHub login token:
    # different files, cleared independently.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    auth.save_token("gh-tok")
    auth.save_api_token("api-tok")
    assert config.api_token_path() != config.token_cache_path()
    auth.clear_token()
    assert auth.load_api_token() == "api-tok"


def test_load_missing_token_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert auth.load_cached_token() == ""


def test_clear_missing_token_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    auth.clear_token()  # must not raise


def test_device_login_requires_client_id():
    with pytest.raises(CliError):
        auth.device_login("")


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_device_login_success(monkeypatch):
    def fake_post(url, headers=None, data=None, timeout=30):
        if url == config.DEVICE_CODE_URL:
            return _Resp({
                "device_code": "dc", "user_code": "UC",
                "verification_uri": "https://verify", "interval": 0, "expires_in": 900,
            })
        return _Resp({"access_token": "gho_token"})

    monkeypatch.setattr(auth.requests, "post", fake_post)
    monkeypatch.setattr(auth.time, "sleep", lambda *_: None)
    assert auth.device_login("Iv1client") == "gho_token"


def test_device_login_surfaces_error(monkeypatch):
    def fake_post(url, headers=None, data=None, timeout=30):
        if url == config.DEVICE_CODE_URL:
            return _Resp({
                "device_code": "dc", "user_code": "UC",
                "verification_uri": "https://verify", "interval": 0, "expires_in": 900,
            })
        return _Resp({"error": "access_denied"})

    monkeypatch.setattr(auth.requests, "post", fake_post)
    monkeypatch.setattr(auth.time, "sleep", lambda *_: None)
    with pytest.raises(CliError, match="access_denied"):
        auth.device_login("Iv1client")
