"""Command-line entry point.

  insula-processor login
  insula-processor create --repo-url https://github.com/<you>/<processor> [options]

Creates a processor on the platform end to end: triggers the cgi-italy pipeline
for a PUBLIC processor repo, waits for it to build/scan/publish, downloads the
produced CWL, and POSTs it to the consuming endpoint using your api token. The
api token is used only for that final POST, locally; it is never sent to GitHub.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid

from . import __version__, auth, config
from .config import Settings
from .errors import CliError
from .github import GitHubClient
from .publish import publish_cwl


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _resolve_secret(flag_value, env_name, prompt, *, required):
    if flag_value:
        return flag_value
    env_value = os.environ.get(env_name)
    if env_value:
        return env_value
    if not required:
        return None
    if not sys.stdin.isatty():
        raise CliError(f"missing secret: set {env_name} or pass it as a flag")
    return getpass.getpass(prompt)


def _resolve_github_token(args):
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
        "no GitHub token: run `insula-processor login`, set "
        f"{config.ENV_GITHUB_TOKEN}, or pass --github-token"
    )


def _resolve_app_client_id(args, settings):
    return (
        getattr(args, "app_client_id", None)
        or os.environ.get(config.ENV_APP_CLIENT_ID)
        or settings.app_client_id
    )


def _build_settings(args) -> Settings:
    settings = Settings()
    path = getattr(args, "config", None) or (
        config.default_config_path()
        if os.path.exists(config.default_config_path())
        else None
    )
    if path:
        settings = config.merge_settings(settings, config.load_config_file(path))
    # Explicit flags win over the config file.
    for name in ("pipeline_repo", "workflow", "upload_field", "auth_header", "auth_format"):
        value = getattr(args, name, None)
        if value:
            setattr(settings, name, value)
    if getattr(args, "endpoint", None):
        settings.publish_endpoint = args.endpoint
    return settings


def _validate_repo_url(url: str) -> None:
    if not url.startswith("https://github.com/"):
        raise CliError("repo-url must be a public https://github.com/<owner>/<repo> URL")


def _cmd_login(args) -> int:
    settings = _build_settings(args)
    token = auth.device_login(_resolve_app_client_id(args, settings))
    auth.save_token(token)
    _log("Logged in. Token cached; `create` will use it automatically.")
    return 0


def _cmd_logout(args) -> int:
    auth.clear_token()
    _log("Logged out; cached token removed.")
    return 0


def _cmd_create(args) -> int:
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
            args.api_token, config.ENV_API_TOKEN, "Publish api token: ", required=True
        )

    client = GitHubClient(github_token, settings.pipeline_repo)
    correlation_id = uuid.uuid4().hex
    inputs = {
        "repo_url": args.repo_url,
        "ref": args.ref,
        "correlation_id": correlation_id,
        "bypass_gate": "true" if args.bypass else "false",
    }

    _log(f"Dispatching {settings.workflow} on {settings.pipeline_repo} ...")
    # Dispatch ref = the pipeline repo's branch (default main). The user's ref is
    # carried inside the workflow inputs, not here.
    client.dispatch(settings.workflow, settings.pipeline_ref, inputs)

    run_id = client.find_run(settings.workflow, correlation_id)
    _log(f"Run started: {client.run_url(run_id)}")
    _log("Waiting for build / scan / publish to finish ...")
    client.wait_for_run(run_id, settings.poll_timeout_seconds, settings.poll_interval_seconds)
    _log("Pipeline succeeded.")

    filename, cwl_bytes = client.download_artifact(run_id, config.CWL_ARTIFACT_NAME)
    if no_publish or args.out:
        out_path = args.out or filename
        with open(out_path, "wb") as handle:
            handle.write(cwl_bytes)
        _log(f"CWL written to {out_path}")

    if no_publish:
        return 0

    _log(f"Publishing CWL to {settings.publish_endpoint} ...")
    response = publish_cwl(settings, api_token, cwl_bytes, filename)
    _log("Published.")
    if response:
        print(response)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="insula-processor", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    login = sub.add_parser("login", help="authenticate via GitHub device flow (no PAT needed)")
    login.add_argument("--app-client-id", help=f"prefer the {config.ENV_APP_CLIENT_ID} env var")
    login.add_argument("--config", help="path to a TOML config file")
    login.set_defaults(func=_cmd_login)

    logout = sub.add_parser("logout", help="remove the cached login token")
    logout.set_defaults(func=_cmd_logout)

    run = sub.add_parser(
        "create", help="build a processor repo and publish its CWL to the platform"
    )
    run.add_argument("--repo-url", required=True, help="public github.com URL of your processor repo")
    run.add_argument("--ref", default="main", help="branch or tag to build (default: main)")
    run.add_argument("--bypass", action="store_true", help="maintainers only: publish despite a failing scan (implies no deploy unless --force-publish)")
    run.add_argument("--force-publish", action="store_true", help="with --bypass, deploy the CWL anyway")
    run.add_argument("--endpoint", help="override the CWL publish endpoint")
    run.add_argument("--pipeline-repo", help=f"default: {config.DEFAULT_PIPELINE_REPO}")
    run.add_argument("--workflow", help=f"default: {config.DEFAULT_WORKFLOW}")
    run.add_argument("--upload-field", help="multipart field name for the CWL upload")
    run.add_argument("--auth-header", help="header name carrying the api token")
    run.add_argument("--auth-format", help="header value template, e.g. 'Bearer {token}'")
    run.add_argument("--github-token", help=f"prefer the {config.ENV_GITHUB_TOKEN} env var or `login`")
    run.add_argument("--api-token", help=f"prefer the {config.ENV_API_TOKEN} env var")
    run.add_argument("--out", help="also write the downloaded CWL to this path")
    run.add_argument("--no-publish", action="store_true", help="build only; skip the deploy")
    run.add_argument("--config", help="path to a TOML config file")
    run.set_defaults(func=_cmd_create)
    return parser


def main(argv=None) -> int:
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
