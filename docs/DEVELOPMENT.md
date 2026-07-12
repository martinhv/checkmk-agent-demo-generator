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

## Strictness vs hop

The backlog from the initial setup has been paid down — the gates are now
clean at full strength:

- **ruff** runs hop's full ruleset with **no per-rule ignores** except `E501`
  (line length): this is a data generator with long embedded agent/SNMP/HTML
  literals where hard-wrapping hurts readability. Everything else is enforced.
- **basedpyright** runs `standard` and is **clean at error level** — no rule
  downgrades; the whole codebase type-checks (`reportUnusedExpression` is even
  promoted to error). The remaining gap to hop's `strict` is annotating every
  param/return and the untyped JSON dicts — a larger effort left for later.
- **pytest** still has no coverage floor (`tests/test_smoke.py` is a scaffold).
  Add `--cov-fail-under=NN` to `pyproject.toml` once real tests exist.

## Coding standards

Match the surrounding code. The `CLAUDE.md` at the repo root is the
engineering knowledge base — read it before changing the fake-agent/SNMP data.
