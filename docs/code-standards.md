# Code standards

Conventions the repository actually follows. The enforcing configuration is in `pyproject.toml`
and `.github/workflows/ci.yml`; this page records the reasons.

## Tooling

| Check | Command | Configuration |
|-------|---------|---------------|
| Lint and import order | `ruff check src tests scripts` | Line length 100, target py311, rules `E F I B UP SIM RUF` |
| Types | `mypy` | `strict = true`, `pydantic.mypy` plugin, checks `src` and `scripts` |
| Tests | `pytest` | `testpaths = tests`, `pythonpath = tests` (tests import each other's builders) |

CI runs all three on Python 3.11 and the tests also on 3.13. Run them before every commit.

## Conventions

- **Strict config models.** Every config model inherits `_Strict` (`extra="forbid"`, `frozen=True`)
  so a mistyped key is a startup error, not a silently ignored policy. Cross-field rules live in
  validators (for example `pii` and `pii_when` are mutually exclusive; roles may only reference
  declared datasets).
- **No secrets in configs.** Values come from the environment through `${VAR}` or
  `${VAR:-default}`; an unset variable without a default fails loading. Backends take the *name* of
  an env var (`dsn_env`, `api_key_env`), never the value. The Postgres and Compose passwords are
  local-only placeholders and are labelled as such.
- **Errors that tell the agent what to do next.** `QueryRejected` (guard) and `BackendError`
  (warehouse) messages are written to be shown to the model: they say what was refused and name the
  available datasets, metrics or columns, or how to narrow the query. `server.py` wraps both in
  `ToolError`. Never put the salt, physical SQL or row-filter text in a message.
- **Fail closed, early.** Unknown functions are denied unless allowlisted
  (`allowed_functions` in config). The server validates declared columns against the warehouse at startup.
- **Policy lives in one path.** New tools call `WarehouseService`; they do not touch a backend directly.
  New backends implement the `Backend` protocol in `backends/base.py` and import optional drivers lazily.
- **Two layers per risk.** A guard rule is paired with an engine-level control where the engine
  offers one. A new guard rule needs a red-team case in `tests/test_red_team.py`.
- **Tests use real engines.** Guard tests execute rewritten SQL on DuckDB. The only fake is the
  Anthropic client in the eval tests. Postgres tests skip, not fail, without the DSN.
- **Comments explain why.** Module docstrings state the design reason (see `sql_guard.py`,
  `redaction.py`); inline comments record a non-obvious invariant. No ticket or phase labels in code,
  tests or commit messages.
- **Files.** `snake_case` modules named for what they own; no module is split without a real boundary.

## Commits

Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `ci:`, `refactor:`), imperative subject, scope
optional (for example `fix(security): ...`). One concern per commit; the body explains the why.

## Documentation

Docs own why and where; code owns what and how. Link to the module, test or config instead of
copying behavior, counts or file lists into prose.
