# Development

The runtime is deliberately **stdlib-only** (the estate runs in place via
`./estate.py`; nothing is pip-installed at runtime). These tools are for
*developing* it and are managed with [uv](https://docs.astral.sh/uv/) — the
same toolchain as `~/git/hop`.

## Tools

- **ruff** — formatting + linting
- **basedpyright** — static type checking
- **pytest** — tests (+ coverage, timeout)
- **prek** — pre-commit hooks that run all of the above

## One-time setup

```bash
./.f12          # uv sync + register the git pre-commit hook
```

or by hand:

```bash
uv sync                 # create .venv with the dev tools (pinned by uv.lock)
uv run prek install     # run the checks on every git commit
```

## Running the checks

```bash
uv run prek run --all-files     # ruff format, ruff check, basedpyright, pytest
```

Individually:

```bash
uv run ruff format              # format
uv run ruff check --fix         # lint (+ autofix)
uv run python -m basedpyright   # type check
uv run python -m pytest         # tests + coverage report
```

CI (`.github/workflows/ci.yml`) runs exactly `uv run prek run --all-files`.

## Two deliberate "green today, tighten later" settings

This code predates the toolchain, so two gates are relaxed from hop's and
carry an explicit backlog — tighten them as the code is cleaned:

- **basedpyright** runs in `standard` (not `strict`); the loose-typing rules
  that currently fire are set to `warning` (still shown, don't fail the gate).
  Promote them back to `error`, then flip to `strict`, as annotations land.
- **ruff** selects hop's full ruleset but `ignore`s the rules that fire on the
  existing code (see the list in `pyproject.toml`). Drop entries as you fix them.
- **pytest** has no coverage floor yet (`tests/test_smoke.py` is a scaffold).
  Add `--cov-fail-under=NN` to `pyproject.toml` once real tests exist.

## Coding standards

Match the surrounding code. The `CLAUDE.md` at the repo root is the
engineering knowledge base — read it before changing the fake-agent/SNMP data.
