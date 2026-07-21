"""Command-line entry point.

  insula-processors-builder login
  insula-processors-builder create --repo-url https://github.com/<you>/<processor> [options]

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
from typing import Optional, Sequence

from . import __version__, auth, config
from .config import Settings
from .errors import CliError
from .github import GitHubClient
from .publish import publish_cwl


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
    slug = url[len("https://github.com/"):].removesuffix(".git").rstrip("/")
    # Exactly <owner>/<repo>: catch a pasted browser URL (.../tree/main) here rather
    # than after a full dispatch + wait ends in a confusing checkout 404.
    if slug.count("/") != 1 or not all(slug.split("/")):
        raise CliError("repo-url must be https://github.com/<owner>/<repo> (no extra path)")


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


def _cmd_deploy(args: argparse.Namespace) -> int:
    """Deploy an already-built CWL to Insula with your api token. Used when a
    maintainer produced the CWL via a --bypass build and you deploy it yourself."""
    settings = _build_settings(args)
    try:
        with open(args.cwl, "rb") as handle:
            cwl_bytes = handle.read()
    except OSError as exc:
        raise CliError(f"cannot read CWL file: {exc}") from exc
    _lint_cwl(cwl_bytes)
    api_token = _resolve_secret(args.api_token, config.ENV_API_TOKEN, "Publish api token: ")
    _log(f"Deploying {args.cwl} to {settings.publish_endpoint} ...")
    response = publish_cwl(settings, api_token, cwl_bytes, os.path.basename(args.cwl))
    _log("Deployed.")
    if response:
        print(response)
    return 0


def _dispatch_and_collect(
    client: GitHubClient, settings: Settings, args: argparse.Namespace
) -> tuple[str, bytes]:
    """Dispatch the pipeline run, wait for it, and return the built CWL artifact."""
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
    # produced the CWL, so tolerate a non-success conclusion and still collect it.
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

    return client.download_artifact(run_id, config.CWL_ARTIFACT_NAME)


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
        api_token = _resolve_secret(args.api_token, config.ENV_API_TOKEN, "Publish api token: ")

    client = GitHubClient(github_token, settings.pipeline_repo)
    filename, cwl_bytes = _dispatch_and_collect(client, settings, args)

    # Persist the CWL BEFORE any deploy attempt: if the POST fails, the build's
    # output survives locally and can be deployed later with `deploy --cwl`,
    # instead of forcing a full pipeline re-run. Name the local file from a
    # CLI-controlled value, not the artifact's own entry name.
    out_path = args.out or "processor.cwl"
    try:
        with open(out_path, "wb") as handle:
            handle.write(cwl_bytes)
    except OSError as exc:
        # The pipeline already ran; do not lose that with a raw traceback.
        raise CliError(
            f"cannot write CWL to {out_path}: {exc} (the CWL is still available "
            f"as the 'cwl' artifact on the run page)"
        ) from exc
    _log(f"CWL written to {out_path}")

    if no_publish:
        return 0

    _lint_cwl(cwl_bytes)
    _log(f"Publishing CWL to {settings.publish_endpoint} ...")
    try:
        response = publish_cwl(settings, api_token, cwl_bytes, filename)
    except CliError as exc:
        raise CliError(
            f"{exc} (the built CWL is saved at {out_path}; deploy it without "
            f"rebuilding via `insula-processors-builder deploy --cwl {out_path}`)"
        ) from exc
    _log("Published.")
    if response:
        print(response)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="insula-processors-builder", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    login = sub.add_parser("login", help="authenticate via GitHub device flow (no PAT needed)")
    login.add_argument("--app-client-id", help=f"prefer the {config.ENV_APP_CLIENT_ID} env var")
    login.add_argument("--config", help="path to a TOML config file")
    login.set_defaults(func=_cmd_login)

    logout = sub.add_parser("logout", help="remove the cached login token")
    logout.set_defaults(func=_cmd_logout)

    dep = sub.add_parser(
        "deploy", help="deploy an already-built CWL file to Insula (e.g. after a maintainer --bypass build)"
    )
    dep.add_argument("--cwl", required=True, help="path to the CWL file to deploy")
    dep.add_argument("--endpoint", help="override the CWL publish endpoint")
    dep.add_argument("--api-token", help=f"prefer the {config.ENV_API_TOKEN} env var")
    dep.add_argument("--auth-header", help="header name carrying the api token")
    dep.add_argument("--auth-format", help="header value template, e.g. 'Bearer {token}'")
    dep.add_argument("--upload-field", help="multipart field name for the CWL upload")
    dep.add_argument("--config", help="path to a TOML config file")
    dep.set_defaults(func=_cmd_deploy)

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
    run.add_argument("--out", help="write the downloaded CWL to this path (default: processor.cwl)")
    run.add_argument("--no-publish", action="store_true", help="build only; skip the deploy")
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
