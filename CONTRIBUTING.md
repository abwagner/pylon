# Contributing to pylon

Thanks for your interest in pylon. Bug reports, doc fixes, test additions, and PRs targeting the issue tracker are all welcome.

## Development setup

pylon targets Python 3.12+. We use [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
git clone https://github.com/abwagner/pylon.git
cd pylon
uv sync
```

Copy `.env.example` to `.env` and set `PLANE_API_KEY` + `GITHUB_WEBHOOK_SECRET` before running anything that hits Plane.

## Running tests

```bash
uv run pytest -q
```

The full suite (≈140 tests) runs in under a second and uses [respx](https://github.com/lundberg/respx) to mock the Plane REST API — no real credentials needed.

## Linting

`ruff` config is in [pyproject.toml](pyproject.toml).

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

## Running pylon locally

```bash
uv run uvicorn src.main:app --reload --port 8000
```

Health check: `curl localhost:8000/healthz`.

## Pull request expectations

- Branch from `main`. Keep PRs focused — one concern per PR.
- Include tests for new behavior. If a change is impossible to unit-test (a deploy-time concern, an external integration), say so in the description.
- Run `uv run pytest -q` locally before opening the PR.
- Write a commit message focused on the "why," not the "what."
- CI runs pytest on every push to `main` and every PR — must pass.

## Reporting bugs and requesting features

Use the GitHub issue templates under [.github/ISSUE_TEMPLATE](.github/ISSUE_TEMPLATE/). For security issues, see [SECURITY.md](SECURITY.md) instead — do not open a public issue.

## Code of conduct

By participating in this project, you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).
