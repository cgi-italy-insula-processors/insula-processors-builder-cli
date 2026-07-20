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
  machine. The CLI downloads the built CWL and does the POST locally. GitHub
  Actions never sees it.
- Base images must be public or on the internal Harbor (the launcher is public, so
  no private base-image credentials are ever passed as workflow inputs).

## Install

```
pipx install insula-processors-builder-cli
```

## Authenticate

Log in once via GitHub device flow (no PAT to create):

```
insula-processors-builder login
```

Or, instead of `login`, set a fine-grained PAT (Contents:write on the launcher
repo only): `export INSULA_GITHUB_TOKEN=github_pat_...`.

## Configure

Copy `config.example.toml` to `~/.config/insula-processors-builder/config.toml` and adjust
the publish endpoint/auth if needed. Provide the deploy token via env:

```
export INSULA_API_TOKEN=...   # your insula.earth api token, for the CWL deploy
```

Missing secrets are prompted for interactively (never echoed).

## Use

Build a repo and deploy its CWL:

```
insula-processors-builder create --repo-url https://github.com/<you>/<processor>
```

Iterate: edit code, `git push`, run again. Each run builds the pushed commit and
produces its own image tag. Run as many times as needed.

Build only, keep the CWL locally, skip deploying (useful while iterating):

```
insula-processors-builder create --repo-url https://github.com/<you>/<processor> --no-publish --out my.cwl
```

## Maintainers: bypass a failing scan

An allowlisted maintainer can publish an image despite a failing secret or
security scan. `--bypass` implies **no deploy** (so you do not accidentally deploy
under your own api token); add `--force-publish` to deploy anyway.

```
insula-processors-builder create --repo-url https://github.com/<user>/<processor> --ref <ref> --bypass
```

The `repo_url` and `ref` for a failed run are shown in that run (and in its
run-name). A non-allowlisted actor using `--bypass` has no effect.

## What a run does

1. Triggers the launcher workflow (`workflow_dispatch`).
2. Waits while the pipeline clones your repo, secret-scans, builds, security-scans,
   and publishes the image.
3. Downloads the CWL artifact (image reference already injected).
4. POSTs the CWL to the endpoint with your api token.

## Workflow contract

The orchestrator workflow must accept these `workflow_dispatch` inputs and set a
`run-name` containing `correlation_id`: `repo_url`, `ref`, `correlation_id`,
`bypass_gate`. It uploads the CWL as an artifact named `cwl`.
