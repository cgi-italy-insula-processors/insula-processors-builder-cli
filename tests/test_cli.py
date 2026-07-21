"""Contract + helper unit tests for the CLI (no network)."""

from __future__ import annotations

import pytest

from insula_processors_builder_cli import cli, config
from insula_processors_builder_cli.errors import CliError, PublishError


def test_workflow_inputs_contract():
    # The launcher's build-external.yml on.workflow_dispatch.inputs must mirror
    # exactly this set. _cmd_create also asserts its dispatch dict against it.
    assert set(config.WORKFLOW_INPUTS) == {"repo_url", "ref", "correlation_id", "bypass_gate"}


def test_cwl_artifact_name_contract():
    # The launcher's finalize_cwl step uploads the artifact under this exact
    # name (its lint.yml pins the other side); renaming either half only fails
    # at a live run's download step.
    assert config.CWL_ARTIFACT_NAME == "cwl"


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

    def download_artifact(self, run_id, name):
        return ("p.cwl", _FAKE_CWL)


def test_create_persists_cwl_before_failed_publish(monkeypatch, tmp_path, capsys):
    # The headline guarantee: the CWL is on disk BEFORE the POST, so a deploy
    # failure costs a retry of `deploy --cwl`, never a full pipeline re-run.
    out = tmp_path / "out.cwl"
    monkeypatch.setattr(cli, "GitHubClient", _FakeGitHubClient)

    def failing_publish(settings, api_token, cwl_bytes, filename):
        raise PublishError("endpoint down")

    monkeypatch.setattr(cli, "publish_cwl", failing_publish)
    monkeypatch.setenv(config.ENV_GITHUB_TOKEN, "gh-token")
    monkeypatch.setenv(config.ENV_API_TOKEN, "api-token")
    # Isolate from any real ~/.config/insula-processors-builder/config.toml.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    rc = cli.main(["create", "--repo-url", "https://github.com/o/r", "--out", str(out)])

    assert rc == 1
    assert out.read_bytes() == _FAKE_CWL
    assert "deploy --cwl" in capsys.readouterr().err
