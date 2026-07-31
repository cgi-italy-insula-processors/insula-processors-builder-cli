"""Contract + helper unit tests for the CLI (no network)."""

from __future__ import annotations

import pytest

from insula_processors_builder_cli import cli, config
from insula_processors_builder_cli.errors import CliError, PublishError


def test_workflow_inputs_contract():
    # The launcher's build-external.yml on.workflow_dispatch.inputs must mirror
    # exactly this set. _cmd_create also asserts its dispatch dict against it.
    assert set(config.WORKFLOW_INPUTS) == {"repo_url", "ref", "correlation_id", "bypass_gate"}


def test_release_tag_prefix_contract():
    # The launcher's finalize_cwl step publishes the CWL as a Release tagged
    # with this prefix + the correlation_id; the CLI looks it up by that tag
    # (its lint.yml pins the launcher side).
    assert config.RELEASE_TAG_PREFIX == "cwl-"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo/",
    ],
)
def test_validate_repo_url_accepts(url):
    cli._validate_repo_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/owner/repo",
        "https://github.com/owner",
        "https://github.com/owner/repo/tree/main",
        "https://gitlab.com/owner/repo",
    ],
)
def test_validate_repo_url_rejects(url):
    with pytest.raises(CliError):
        cli._validate_repo_url(url)


def test_lint_cwl_accepts_minimal():
    cwl = b"""$graph:
- class: Workflow
- class: CommandLineTool
  requirements:
    DockerRequirement:
      dockerPull: swr.example/eopaas/eopaas/foo:abc12345
"""
    cli._lint_cwl(cwl)


@pytest.mark.parametrize(
    "cwl",
    [
        b"class: Workflow\nclass: CommandLineTool\ndockerPull: __IMAGE__\n",  # token left
        b"class: Workflow\ndockerPull: x\n",  # missing CommandLineTool
        b"class: Workflow\nclass: CommandLineTool\n",  # missing dockerPull
        b"class: Workflow\nclass: Workflow\nclass: CommandLineTool\ndockerPull: x\n",  # two Workflows
    ],
)
def test_lint_cwl_rejects(cwl):
    with pytest.raises(CliError):
        cli._lint_cwl(cwl)


_FAKE_CWL = (
    b"$graph:\n"
    b"- class: Workflow\n"
    b"- class: CommandLineTool\n"
    b"  requirements:\n"
    b"    DockerRequirement:\n"
    b"      dockerPull: reg/eopaas/eopaas/foo:abc12345\n"
)


_FAKE_URL = "https://github.com/cgi-italy/insula-processor-launcher/releases/download/cwl-x/app.cwl"


class _FakeGitHubClient:
    def __init__(self, token, repo):
        pass

    def dispatch(self, workflow, ref, inputs):
        pass

    def find_run(self, workflow, correlation_id):
        return 1

    def run_url(self, run_id):
        return "https://example.invalid/run/1"

    def wait_for_run(self, run_id, timeout, interval, allow_failure=False):
        return "success"

    def get_cwl_url(self, correlation_id):
        return _FAKE_URL


def _prep_create(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "GitHubClient", _FakeGitHubClient)
    # The CWL is fetched (for the local lint) from its published URL, not downloaded
    # as an artifact; stub the fetch so no network is touched.
    monkeypatch.setattr(cli, "fetch_cwl", lambda settings, url: _FAKE_CWL)
    monkeypatch.setenv(config.ENV_GITHUB_TOKEN, "gh-token")
    monkeypatch.setenv(config.ENV_API_TOKEN, "api-token")
    # Isolate from any real ~/.config/insula-processors-builder/config.toml.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_create_failed_publish_points_at_deploy_by_url(monkeypatch, tmp_path, capsys):
    # On a deploy failure the CWL is already at a durable public URL, so recovery
    # is `deploy --cwl-url <url>`, never a full pipeline re-run.
    _prep_create(monkeypatch, tmp_path)

    def failing_publish(settings, api_token, cwl_url):
        raise PublishError("endpoint down")

    monkeypatch.setattr(cli, "publish_cwl", failing_publish)

    rc = cli.main(["create", "--repo-url", "https://github.com/o/r"])

    assert rc == 1
    err = capsys.readouterr().err
    assert f"deploy --cwl-url {_FAKE_URL}" in err


def test_create_no_publish_prints_url(monkeypatch, tmp_path, capsys):
    # --no-publish builds only and prints the durable CWL URL for a later deploy.
    _prep_create(monkeypatch, tmp_path)

    rc = cli.main(["create", "--repo-url", "https://github.com/o/r", "--no-publish"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == _FAKE_URL


def test_create_bypass_skips_deploy(monkeypatch, tmp_path, capsys):
    # --bypass implies no deploy (do not publish under a maintainer's own token);
    # it prints the URL and never calls publish_cwl.
    _prep_create(monkeypatch, tmp_path)
    called = {"pub": False}
    monkeypatch.setattr(cli, "publish_cwl", lambda *a, **k: called.__setitem__("pub", True))

    rc = cli.main(["create", "--repo-url", "https://github.com/o/r", "--bypass"])

    assert rc == 0
    assert called["pub"] is False
    assert capsys.readouterr().out.strip() == _FAKE_URL


def test_deploy_by_url_posts_that_url(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv(config.ENV_API_TOKEN, "api-token")
    # fetch_cwl (for the local lint) and publish_cwl are stubbed - no network.
    monkeypatch.setattr(cli, "fetch_cwl", lambda settings, url: _FAKE_CWL)
    captured = {}
    monkeypatch.setattr(cli, "publish_cwl", lambda settings, tok, url: captured.update(url=url) or "ok")

    rc = cli.main(["deploy", "--cwl-url", "https://x/app.cwl"])

    assert rc == 0
    assert captured["url"] == "https://x/app.cwl"


def test_deploy_rejects_abbreviated_cwl_flag(monkeypatch, tmp_path):
    # allow_abbrev=False: --cwl must NOT bind to --cwl-url; argparse errors (exit 2).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(SystemExit):
        cli.main(["deploy", "--cwl", "https://x/app.cwl"])


_AUTHOR_CWL = (
    b"$graph:\n"
    b"- class: Workflow\n"
    b"- class: CommandLineTool\n"
    b"  requirements:\n"
    b"    DockerRequirement:\n"
    b"      dockerPull: __IMAGE__\n"
)


def test_validate_accepts_author_cwl(tmp_path):
    p = tmp_path / "p.cwl"
    p.write_bytes(_AUTHOR_CWL)
    assert cli.main(["validate", "--cwl", str(p)]) == 0


def test_validate_rejects_missing_image_token(tmp_path):
    # Author pre-check requires the __IMAGE__ token present (the reverse of _lint_cwl).
    p = tmp_path / "p.cwl"
    p.write_bytes(_AUTHOR_CWL.replace(b"__IMAGE__", b"reg/foo:tag"))
    assert cli.main(["validate", "--cwl", str(p)]) == 1


def test_deploy_rejects_non_http_url(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv(config.ENV_API_TOKEN, "api-token")
    assert cli.main(["deploy", "--cwl-url", "file:///etc/passwd"]) == 1


def test_deploy_rejects_bad_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv(config.ENV_API_TOKEN, "api-token")
    assert cli.main(["deploy", "--cwl-url", "https://x/app.cwl", "--endpoint", "notaurl"]) == 1
