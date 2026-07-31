"""Command-line entry point.

  insula-processors-builder login
  insula-processors-builder create --repo-url https://github.com/<you>/<processor> [options]

Creates a processor on the platform end to end: triggers the cgi-italy pipeline
for a PUBLIC processor repo, waits for it to build/scan/publish, then deploys the
CWL the pipeline published (Insula fetches it by URL) using your api token. The
api token is used only for that final deploy, locally; it is never sent to GitHub.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid
from typing import Optional, Sequence
from urllib.parse import urlparse

from . import __version__, auth, config
from .config import Settings
from .errors import CliError
from .github import GitHubClient
from .publish import fetch_cwl, publish_cwl


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _resolve_secret(flag_value: Optional[str], env_name: str, prompt: str) -> str:
    if flag_value:
        return flag_value
    env_value = os.environ.get(env_name)
    if env_value:
        return env_value
    if not sys.stdin.isatty():
        raise CliError(f"missing secret: set {env_name} or pass it as a flag")
    return getpass.getpass(prompt)


def _resolve_github_token(args: argparse.Namespace) -> str:
    """Order: --github-token, INSULA_GITHUB_TOKEN, then the `login` token cache."""
    if getattr(args, "github_token", None):
        return args.github_token
    env_value = os.environ.get(config.ENV_GITHUB_TOKEN)
    if env_value:
        return env_value
    cached = auth.load_cached_token()
    if cached:
        return cached
    raise CliError(
        "no GitHub token: run `insula-processors-builder login`, set "
        f"{config.ENV_GITHUB_TOKEN}, or pass --github-token"
    )


def _resolve_app_client_id(args: argparse.Namespace, settings: Settings) -> Optional[str]:
    return (
        getattr(args, "app_client_id", None)
        or os.environ.get(config.ENV_APP_CLIENT_ID)
        or settings.app_client_id
    )


def _build_settings(args: argparse.Namespace) -> Settings:
    settings = Settings()
    path = getattr(args, "config", None) or (
        config.default_config_path()
        if os.path.exists(config.default_config_path())
        else None
    )
    if path:
        try:
            data = config.load_config_file(path)
        except (OSError, ValueError) as exc:
            # ValueError covers tomllib.TOMLDecodeError; keep it a clean CliError.
            raise CliError(f"cannot read config file {path}: {exc}") from exc
        settings = config.merge_settings(settings, data)
    # Explicit flags win over the config file.
    for name in ("pipeline_repo", "workflow"):
        value = getattr(args, name, None)
        if value:
            setattr(settings, name, value)
    if getattr(args, "endpoint", None):
        settings.publish_endpoint = args.endpoint
    if settings.publish_endpoint:
        _validate_url(settings.publish_endpoint, "publish endpoint")
    if getattr(args, "insecure", False):
        settings.verify_tls = False
    # Warn whenever verification is off, no matter the source: a config-file
    # verify_tls=false must not silently disable TLS on the token-bearing POST.
    if not settings.verify_tls:
        source = "--insecure" if getattr(args, "insecure", False) else "verify_tls=false in config"
        _log(f"warning: TLS verification disabled for the deploy endpoint ({source})")
    return settings


def _validate_repo_url(url: str) -> None:
    if not url.startswith("https://github.com/"):
        raise CliError("repo-url must be a public https://github.com/<owner>/<repo> URL")
    slug = url[len("https://github.com/"):].removesuffix(".git").rstrip("/")
    # Exactly <owner>/<repo>: catch a pasted browser URL (.../tree/main) here rather
    # than after a full dispatch + wait ends in a confusing checkout 404.
    if slug.count("/") != 1 or not all(slug.split("/")):
        raise CliError("repo-url must be https://github.com/<owner>/<repo> (no extra path)")


def _validate_url(url: str, what: str) -> None:
    """Reject a non-http(s) or hostless URL. The publish endpoint carries the api
    token and the CWL URL is fetched, so both must be real web URLs, not file://
    or a bare path."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CliError(f"{what} must be an http(s) URL, got: {url}")


def _lint_cwl(cwl_bytes: bytes) -> None:
    """Cheap client-side sanity check before deploying, so obvious CWL mistakes fail
    locally instead of after a slow server-side rejection. Not a full validator; the
    platform still validates on deploy."""
    try:
        text = cwl_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliError(f"CWL is not valid UTF-8: {exc}") from exc
    problems = []
    if text.count("class: Workflow") != 1:
        problems.append("exactly one 'class: Workflow' is required")
    if text.count("class: CommandLineTool") != 1:
        problems.append("exactly one 'class: CommandLineTool' is required")
    if "dockerPull" not in text:
        problems.append("a DockerRequirement.dockerPull is required")
    if "__IMAGE__" in text:
        problems.append("the __IMAGE__ token is still present (image was not injected)")
    if problems:
        raise CliError("CWL failed local checks: " + "; ".join(problems))


def _validate_cwl_source(cwl_bytes: bytes) -> None:
    """Structural check for an AUTHOR's local .cwl BEFORE building: one Workflow, one
    CommandLineTool, a dockerPull, and exactly one `__IMAGE__` token still present.
    Unlike `_lint_cwl` (which runs post-build and requires the token to be GONE),
    this expects the token, since the pipeline injects the image later."""
    try:
        text = cwl_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliError(f"CWL is not valid UTF-8: {exc}") from exc
    problems = []
    if text.count("class: Workflow") != 1:
        problems.append("exactly one 'class: Workflow' is required")
    if text.count("class: CommandLineTool") != 1:
        problems.append("exactly one 'class: CommandLineTool' is required")
    if "dockerPull" not in text:
        problems.append("a DockerRequirement.dockerPull is required")
    tokens = text.count("__IMAGE__")
    if tokens != 1:
        problems.append(
            f"exactly one __IMAGE__ token is required (found {tokens}); the pipeline "
            "injects the published image there"
        )
    if problems:
        raise CliError("CWL failed local checks: " + "; ".join(problems))


def _cmd_validate(args: argparse.Namespace) -> int:
    """Lint a local .cwl before building, so structural mistakes fail on your
    machine instead of after a full pipeline run."""
    try:
        with open(args.cwl, "rb") as handle:
            cwl_bytes = handle.read()
    except OSError as exc:
        raise CliError(f"cannot read CWL file: {exc}") from exc
    _validate_cwl_source(cwl_bytes)
    _log(f"{args.cwl} passed local CWL checks.")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    settings = _build_settings(args)
    token = auth.device_login(_resolve_app_client_id(args, settings))
    auth.save_token(token)
    _log("Logged in. Token cached; `create` will use it automatically.")
    return 0


def _cmd_logout(args: argparse.Namespace) -> int:
    auth.clear_token()
    _log("Logged out; cached token removed.")
    return 0


def _deploy_url(settings: Settings, api_token: str, cwl_url: str) -> int:
    """Lint the referenced CWL, then deploy it by URL. Shared by create and deploy."""
    _validate_url(cwl_url, "CWL URL")
    _lint_cwl(fetch_cwl(settings, cwl_url))
    _log(f"Deploying {cwl_url} to {settings.publish_endpoint} ...")
    response = publish_cwl(settings, api_token, cwl_url)
    _log("Deployed.")
    if response:
        print(response)
    return 0


def _cmd_deploy(args: argparse.Namespace) -> int:
    """Deploy an already-built CWL to Insula with your api token, given its public
    URL. Used when a maintainer produced the CWL via a --bypass build and shares
    the release URL for you to deploy under your own api token."""
    settings = _build_settings(args)
    api_token = _resolve_secret(
        args.api_token,
        config.ENV_API_TOKEN,
        "Provide Insula API Token (input hidden; generate one at "
        "https://insula.earth/awareness/account/api_keys): ",
    )
    return _deploy_url(settings, api_token, args.cwl_url)


def _dispatch_and_collect(
    client: GitHubClient, settings: Settings, args: argparse.Namespace
) -> str:
    """Dispatch the pipeline run, wait for it, and return the published CWL URL."""
    correlation_id = uuid.uuid4().hex
    inputs = {
        "repo_url": args.repo_url,
        "ref": args.ref,
        "correlation_id": correlation_id,
        "bypass_gate": "true" if args.bypass else "false",
    }
    # Guard the dispatch contract from drift: the keys we send MUST match the
    # documented WORKFLOW_INPUTS (which the launcher's inputs mirror).
    if set(inputs) != set(config.WORKFLOW_INPUTS):
        raise CliError("internal: dispatch inputs drifted from the WORKFLOW_INPUTS contract")

    _log(f"Dispatching {settings.workflow} on {settings.pipeline_repo} ...")
    # Dispatch ref = the pipeline repo's branch (default main). The user's ref is
    # carried inside the workflow inputs, not here.
    client.dispatch(settings.workflow, settings.pipeline_ref, inputs)

    run_id = client.find_run(settings.workflow, correlation_id)
    _log(f"Run started: {client.run_url(run_id)}")
    _log("Waiting for build / scan / publish to finish ...")
    # Under --bypass the run concludes 'failure' (a scan failed) yet publish ran and
    # published the CWL, so tolerate a non-success conclusion and still collect it.
    conclusion = client.wait_for_run(
        run_id,
        settings.poll_timeout_seconds,
        settings.poll_interval_seconds,
        allow_failure=args.bypass,
    )
    if conclusion == "success":
        _log("Pipeline succeeded.")
    else:
        _log(f"Pipeline concluded '{conclusion}'; continuing under --bypass to collect the CWL.")

    return client.get_cwl_url(correlation_id)


def _cmd_create(args: argparse.Namespace) -> int:
    _validate_repo_url(args.repo_url)
    settings = _build_settings(args)

    # A bypass defaults to NOT publishing, so a maintainer relaunching a failed
    # build does not accidentally deploy the CWL under their own api token.
    # --force-publish overrides that.
    no_publish = args.no_publish or (args.bypass and not args.force_publish)
    if args.bypass and no_publish and not args.no_publish:
        _log("bypass set: skipping the CWL deploy (use --force-publish to deploy anyway)")

    github_token = _resolve_github_token(args)
    # Resolve the api token up front (unless skipping publish) so we fail before
    # spending a full pipeline run on a missing credential.
    api_token = None
    if not no_publish:
        api_token = _resolve_secret(
            args.api_token,
            config.ENV_API_TOKEN,
            "Provide Insula API Token (input hidden; generate one at "
            "https://insula.earth/awareness/account/api_keys): ",
        )

    client = GitHubClient(github_token, settings.pipeline_repo)
    cwl_url = _dispatch_and_collect(client, settings, args)
    # The finalized CWL lives at a durable public URL; no local copy needed. A
    # failed deploy is retried with `deploy --cwl-url <url>`, not a pipeline re-run.
    _log(f"CWL published at {cwl_url}")

    if no_publish:
        print(cwl_url)
        return 0

    try:
        return _deploy_url(settings, api_token, cwl_url)
    except CliError as exc:
        raise CliError(
            f"{exc} (the built CWL is published at {cwl_url}; deploy it without "
            f"rebuilding via `insula-processors-builder deploy --cwl-url {cwl_url}`)"
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False: a prefix like `--cwl` must NOT silently bind to `--cwl-url`
    # (an unrecognized flag should error, not resolve to a longer one). argparse does
    # not propagate this to subparsers, so each add_parser sets it too.
    parser = argparse.ArgumentParser(
        prog="insula-processors-builder", description=__doc__, allow_abbrev=False
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    login = sub.add_parser("login", help="authenticate via GitHub device flow (no PAT needed)", allow_abbrev=False)
    login.add_argument("--app-client-id", help=f"prefer the {config.ENV_APP_CLIENT_ID} env var")
    login.add_argument("--config", help="path to a TOML config file")
    login.set_defaults(func=_cmd_login)

    logout = sub.add_parser("logout", help="remove the cached login token", allow_abbrev=False)
    logout.set_defaults(func=_cmd_logout)

    val = sub.add_parser(
        "validate", help="check a local .cwl for the Insula structure before building",
        allow_abbrev=False,
    )
    val.add_argument("--cwl", required=True, help="path to the local .cwl to check")
    val.set_defaults(func=_cmd_validate)

    dep = sub.add_parser(
        "deploy", help="deploy an already-built CWL to Insula by its public URL (e.g. after a maintainer --bypass build)",
        allow_abbrev=False,
    )
    dep.add_argument("--cwl-url", required=True, help="public URL of the CWL to deploy (Insula fetches it)")
    dep.add_argument("--endpoint", help="override the CWL publish endpoint")
    dep.add_argument("--api-token", help=f"prefer the {config.ENV_API_TOKEN} env var")
    dep.add_argument("--insecure", action="store_true", help="skip TLS verification of the deploy endpoint (behind a corporate TLS-inspecting proxy)")
    dep.add_argument("--config", help="path to a TOML config file")
    dep.set_defaults(func=_cmd_deploy)

    run = sub.add_parser(
        "create", help="build a processor repo and publish its CWL to the platform",
        allow_abbrev=False,
    )
    run.add_argument("--repo-url", required=True, help="public github.com URL of your processor repo")
    run.add_argument("--ref", default="main", help="branch or tag to build (default: main)")
    run.add_argument("--bypass", action="store_true", help="maintainers only: publish despite a failing scan (implies no deploy unless --force-publish)")
    run.add_argument("--force-publish", action="store_true", help="with --bypass, deploy the CWL anyway")
    run.add_argument("--endpoint", help="override the CWL publish endpoint")
    run.add_argument("--pipeline-repo", help=f"default: {config.DEFAULT_PIPELINE_REPO}")
    run.add_argument("--workflow", help=f"default: {config.DEFAULT_WORKFLOW}")
    run.add_argument("--github-token", help=f"prefer the {config.ENV_GITHUB_TOKEN} env var or `login`")
    run.add_argument("--api-token", help=f"prefer the {config.ENV_API_TOKEN} env var")
    run.add_argument("--no-publish", action="store_true", help="build only; print the published CWL URL and skip the deploy")
    run.add_argument("--insecure", action="store_true", help="skip TLS verification of the deploy endpoint (behind a corporate TLS-inspecting proxy)")
    run.add_argument("--config", help="path to a TOML config file")
    run.set_defaults(func=_cmd_create)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    try:
        return args.func(args)
    except CliError as exc:
        _log(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        _log("aborted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
