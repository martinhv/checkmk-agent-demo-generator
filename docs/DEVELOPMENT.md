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

- **ruff** runs hop's full ruleset with **no per-rule ignores** — `E501`
  included (the long embedded agent/SNMP/HTML literals were wrapped). Only a
  `tests/*` `E402` exception remains (they import repo modules after a
  `sys.path` insert).
- **basedpyright** now runs full **`strict`** (hop's tier) and is **clean —
  0 errors, 0 warnings**. The annotation-coverage backlog is paid down: every
  param/return is annotated and the untyped JSON navigation is typed via small
  read-only `_ED`/`_EL` sentinels (for `dict.get(...)` defaults) and a couple
  of `cast`s. Exactly **one** strict rule is turned off — `reportConstantRedefinition`
  — because the runtime deliberately keeps process-global state in `UPPER_CASE`
  module names that reassign (netsim `START`/`DEVICES`, each fake agent's toggle
  state); the "constants aren't reassigned" heuristic is wrong for them.
- **pytest** still has no coverage floor (`tests/test_smoke.py` is a scaffold).
  Add `--cov-fail-under=NN` to `pyproject.toml` once real tests exist.

## Coding standards

Match the surrounding code. The `CLAUDE.md` at the repo root is the
engineering knowledge base — read it before changing the fake-agent/SNMP data.
