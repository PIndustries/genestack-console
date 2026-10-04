# Contributing

Genestack Console is the program you install on a Linux server outside the cluster. This repository is that program. Pull requests land on `main`.

`main` is the development line. The install command follows the latest GitHub Release, not `main`. Merging a pull request does not change what `curl -fsSL https://get.genestack.dev/console.sh | bash` installs.

## Open a pull request

1. Fork `PIndustries/genestack-console`.
2. Create a branch from `main`.
3. Add a test for the behavior you changed.
4. Open the pull request against `main`.

Do not open a pull request against a tag. A tag is a release that has already been cut.

The pull request runs the test suite on a GitHub-hosted runner. It does not build the Linux binary, and it does not use the fleet machines that build a release.

A maintainer reviews a pull request from someone else. After the tests pass, they merge it. Maintainers sign their own commits. A signature is not required from every contributor.

## What a release is

A release is a git tag `v` plus `year.month.day.build` in `app/version.py`, for example `v2026.10.04.1`. Pushing that tag builds the binary and moves the install command. The steps are in [docs/releasing.md](docs/releasing.md).

A tag with a hyphen, such as `v2026.10.04-rc.1`, is a prerelease. It does not become the latest release, so the install command stays where it is.

A release branch exists only when a fix has to ship for an older tag without the rest of `main`. Cut it from that tag, name it `release/YYYY.MM`, put the fix on `main` first, then cherry-pick it onto the release branch and tag from there.

## Run it locally

Python 3.11 or newer. From a checkout of this repository:

```bash
uv run --extra dev uvicorn app.main:app --reload --app-dir .
```

Open `http://127.0.0.1:8000/ui`.

`uv run --extra dev` reads `pyproject.toml`. The dev extra installs pytest. `config.yaml` is local. `python3 -m app.cli make-config` writes one. Do not commit that file, an API key, a kubeconfig, or a password.

## Tests

```bash
uv run --extra dev pytest -q --tb=line
```

The same command runs on a pull request.

## Style

Match the code around the change. Python lines stay at 100 characters. Ruff is the linter in `pyproject.toml`. Type the public functions. Do not reformat the whole repository in a pull request.
