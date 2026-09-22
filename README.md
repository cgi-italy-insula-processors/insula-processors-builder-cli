# insula-processors-builder-cli

Launch the cgi-italy processor build pipeline for your PUBLIC processor repo,
then deploy the produced CWL to Insula. No server to run, no web form: everything
happens from your machine.

Your processor repo stays public and under your own name. The pipeline itself
lives in a cgi-italy repo you cannot alter; you are granted access only to
*trigger* it.

## Security model

- You get **write** on the launcher repo only (needed to dispatch; GitHub has no
  dispatch-only permission). A ruleset + CODEOWNERS on its default branch stop you
  from changing what the pipeline runs, and registry secrets live in an
  environment gated to that branch, so you cannot read them.
- `workflow_dispatch` runs the launcher's default-branch workflow, so you cannot
  substitute your own pipeline.
- Your **api token** (used to authenticate the CWL deploy) never leaves your
  machine. The CLI reads the published CWL's release URL and does the deploy POST
  locally (Insula fetches the CWL from that URL). GitHub Actions never sees the token.
- Base images must be public (the launcher is public, so no private base-image
  credentials are ever passed as workflow inputs).

## Prerequisites: Python 3.11+ and pipx

The CLI is installed with [pipx](https://pipx.pypa.io), which puts it in its own
virtual environment and on your PATH. If `pipx --version` already prints a version,
skip to [Install](#install).

**Windows**

```
winget install -e --id Python.Python.3.12   # only if `py --version` fails
py -m pip install --user pipx
py -m pipx ensurepath
```

Close and reopen the terminal (`ensurepath` edits your PATH; the current window
does not see it), then check `pipx --version`. If the command is still not found,
use `py -m pipx ...` in place of `pipx ...`.
Scoop users can instead run `scoop install pipx`.

**Linux**

```
sudo apt install pipx        # Debian 12+ / Ubuntu 23.04+
sudo dnf install pipx        # Fedora
python3 -m pip install --user pipx   # any other distro (needs python3 3.11+)

pipx ensurepath
exec $SHELL                  # reload PATH in the current shell
```

Check with `pipx --version`. On older distributions whose `python3` is below 3.11,
install a newer Python first (for example `sudo apt install python3.11`) and use
`python3.11 -m pip install --user pipx`.

## Install

Requires Python 3.11+. The CLI is not on PyPI; install straight from the repo:

```
pipx install git+https://github.com/cgi-italy-insula-processors/insula-processors-builder-cli
```

Upgrade later with `pipx upgrade insula-processors-builder-cli`,
remove it with `pipx uninstall insula-processors-builder-cli`,
force reinstall with `pipx install -f insula-processors-builder-cli`.

## Authenticate

**A maintainer must grant you access first.** `login` succeeds for ANY GitHub
account, so it is not a signal that you can build: `create` fails at dispatch with
`404 Not Found` until a maintainer adds you to the launcher repo. Get onboarded
before your first `create`.

Log in via GitHub device flow (no PAT to create):

```
insula-processors-builder login
```

The login token expires after about 8 hours; when a `create` fails with an auth
error, run `login` again. To avoid re-logging in, set a fine-grained PAT instead
(Actions: read/write on the launcher repo only): `export INSULA_GITHUB_TOKEN=github_pat_...`.

## Store your Insula api token

The CWL deploy authenticates with an Insula api token (generate one at
https://insula.earth/awareness/account/api_keys). Store it once; the CLI then uses
it automatically:

```
insula-processors-builder set-api-token
```

The command asks for the token and does not echo it, so the value never passes
through your shell (api tokens contain characters a shell would otherwise mangle
unless carefully quoted). It is written to
`~/.config/insula-processors-builder/api-token` with mode 0600. Remove it with
`insula-processors-builder clear-api-token`.

Alternatives, in the order the CLI tries them: `--api-token <value>`, the
`INSULA_API_TOKEN` environment variable (quote the value: `export
INSULA_API_TOKEN="..."`), the stored token, then an interactive prompt.

There is **no settings file to edit**: every other setting is a built-in default you
can override with a command-line flag (`--endpoint`, `--insecure`, `--poll-timeout`,
`--poll-interval`, `--pipeline-repo`, `--workflow`, `--app-client-id`). The deploy
Content-Type (`application/ogcapppkg+json`) and `Authorization: Apikey` scheme are
fixed, not configurable.

## Use

Build a repo and deploy its CWL:

```
insula-processors-builder create --repo-url https://github.com/<you>/<processor>
```

Iterate: edit code, `git push`, run again. Each run builds the pushed commit and
produces its own image tag. Run as many times as needed.

Note: runs are keyed by repo + ref. Dispatching the same repo and ref again while a
run is in flight CANCELS the older run (the CLI then reports its conclusion as
`cancelled`). Let a run finish, or build a different ref, if you do not want that.

Build only, skip deploying (useful while iterating). The finalized CWL is published
at a durable public URL, which the CLI prints on stdout for a later `deploy`:

```
insula-processors-builder create --repo-url https://github.com/<you>/<processor> --no-publish
```

## Maintainers: bypass a failing scan

A maintainer (maintain/admin role on the launcher repo, team grants included) can
publish an image despite a failing secret or security scan. `--bypass` implies
**no deploy** (so you do not accidentally deploy under your own api token); add
`--force-publish` to deploy anyway.

```
insula-processors-builder create --repo-url https://github.com/<user>/<processor> --ref <ref> --bypass
```

The `repo_url` and `ref` for a failed run are shown in that run (and in its
run-name). Any other actor using `--bypass` has no effect.

## Deploy a CWL a maintainer built for you

If a maintainer had to force your build (a `--bypass` run, e.g. to get an image past
a scan) they hand you the published CWL URL (the `create` output, a Release asset on
the launcher repo). Deploy it under your OWN api token, with no rebuild:

```
insula-processors-builder deploy --cwl-url https://github.com/cgi-italy/insula-processor-launcher/releases/download/cwl-<id>/<app>-<sha8>.cwl
```

Generate the api token at https://insula.earth/awareness/account/api_keys and store it
with `insula-processors-builder set-api-token` (or set `INSULA_API_TOKEN`, or let the
CLI prompt for it; a typed or pasted token is not shown in the terminal). The token is
used only for this local POST and is never sent to GitHub.

## Behind a corporate TLS-inspecting proxy

Corporate firewalls that inspect TLS re-sign HTTPS connections with an internal CA the
CLI does not trust, so the deploy POST fails with a certificate verification error. Pass `--insecure` (on `create` or `deploy`) to skip
verification for that request. This affects only the deploy endpoint, not GitHub.

```
insula-processors-builder deploy --cwl-url <url> --insecure
```

The cleaner alternative, if your IT provides the proxy's root CA bundle, is to point
`requests` at it instead of disabling verification: `export REQUESTS_CA_BUNDLE=/path/to/corp-ca.pem`.

## Command reference

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `login` | Cache a GitHub device-flow token | `--app-client-id` |
| `logout` | Remove the cached login token | - |
| `set-api-token` | Store the Insula api token locally (mode 0600) | `--api-token` (otherwise asked for, never echoed) |
| `clear-api-token` | Remove the stored Insula api token | - |
| `validate --cwl <file>` | Run the [CWL checks](#cwl-checks-run-locally-before-anything-is-built) on a local .cwl (with `__IMAGE__`) before building | - |
| `create --repo-url <url>` | Build a processor repo and deploy its CWL | `--ref`, `--no-publish`, `--endpoint`, `--insecure`, `--bypass`, `--force-publish`, `--poll-timeout`, `--poll-interval`, `--pipeline-repo`, `--workflow`, `--github-token`, `--api-token` |
| `deploy --cwl-url <url>` | Deploy an already-published CWL by its URL | `--endpoint`, `--insecure`, `--api-token` |

Environment variables: `INSULA_GITHUB_TOKEN` (GitHub token, skips `login`),
`INSULA_API_TOKEN` (Insula deploy token), `INSULA_GITHUB_APP_CLIENT_ID`,
`XDG_CONFIG_HOME` (where the two token files live), `REQUESTS_CA_BUNDLE` (custom CA
bundle).

Token resolution: GitHub token = `--github-token` > `INSULA_GITHUB_TOKEN` > cached
`login`. api token = `--api-token` > `INSULA_API_TOKEN` > stored `set-api-token` >
interactive prompt.

Exit codes: `0` success, `1` handled error, `2` no subcommand (help printed),
`130` interrupted. `create --no-publish` prints the published CWL URL on stdout (all
other logs go to stderr), so it is safe to capture in a script.

## CWL checks (run locally, before anything is built)

`create` reads the `.cwl` straight from your repo at the ref it is about to build
and checks it BEFORE dispatching the pipeline; `validate --cwl <file>` runs the same
checks on a local file. A finding stops the command and lists every problem.

This matters because Insula rejects a malformed Application Package with an HTTP 400
that carries NO reason (the explanation stays in the platform's server logs). Without
these local checks you would learn only "400 BAD_REQUEST" - after a full build, scan
and publish cycle.

What is checked, mirroring the platform's own rules:

- exactly one `Workflow` and one `CommandLineTool` in `$graph`, both with an `id`
- exactly one step, whose `run` references the CommandLineTool id
- `DockerRequirement.dockerPull` present, and still the bare `__IMAGE__` token
  before a build (the pipeline injects the published image there), appearing exactly
  once across the CWL's keys and values - the pipeline substitutes globally, so a
  second occurrence in a real value would receive the image reference too. A comment
  that merely names the token is not counted
- only the supported CommandLineTool requirements (`DockerRequirement`,
  `ResourceRequirement`, `NetworkAccess`, `EnvVarRequirement`,
  `InitialWorkDirRequirement`), each `InitialWorkDirRequirement` Directory carrying a
  `location` and a relative `basename`
- the Workflow `doc` is a single string of at most 255 characters (it becomes the
  process description, which the platform caps)
- every type is a real CWL type, spelled exactly: `string`, `int`, `long`, `float`,
  `double`, `boolean`, `File`, `Directory`, an enum or an array of those. `String`
  is not `string`
- the Workflow and the CommandLineTool declare the SAME type for each input (with
  `scatter`, the Workflow side is the array of the tool side)
- inputs and outputs line up across Workflow, step and CommandLineTool
- outputs are `File` or `Directory` only
- with `scatter`: `scatterMethod: dotproduct`, an array scatter input, array outputs
- no duplicate keys anywhere (the platform's YAML parser rejects them)

Rules that depend on platform state - whether a process of that name already exists,
whether a named user mount is known - cannot be checked locally and are still only
enforced on deploy.

## What a run does

0. Reads the single `.cwl` from your repo (root or one directory down, the same
   lookup the pipeline uses) and runs the checks above. Nothing is dispatched if it
   fails.
1. Triggers the launcher workflow (`workflow_dispatch`).
2. Waits while the pipeline clones your repo, secret-scans, builds, security-scans,
   and publishes the image.
3. The pipeline publishes the finalized CWL (image reference already injected) as a
   GitHub Release on the launcher repo, tagged `cwl-<correlation_id>`. The CLI reads
   that release's asset URL - a durable public link, so a failed deploy never costs
   you the build: retry later with `insula-processors-builder deploy --cwl-url <url>`.
4. Deploys the CWL to Insula by reference: it sends `{ executionUnit: { href } }`
   with your api token, and Insula fetches the CWL from that URL. Transient failures
   (connection errors, 429/502/503) are retried a few times; anything else -
   including 504 and read timeouts, where the deploy may still have gone through -
   is reported, with the CWL URL printed for a later `deploy`.

## Workflow contract

The orchestrator workflow must accept these `workflow_dispatch` inputs and set a
`run-name` containing `correlation_id`: `repo_url`, `ref`, `correlation_id`,
`bypass_gate`. It publishes the finalized CWL as a Release tagged
`cwl-<correlation_id>` whose single asset is the CWL file.
