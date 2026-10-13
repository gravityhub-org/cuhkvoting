# AGENTS.md

Guidance for agents working in the `cuhkvoting` repository.

## What this is

A minimal Python CLI (`cuhkvoting`) to browse arXiv papers and vote on them. Paper
metadata lives in a `papers/` tree; votes and journal-club selections are written to a
**remote GitHub records repo** (`gravityhub-org/cuhkvoting-records` by default). The
repo you are in right now is the *client*; the `papers/*.json` files checked in here are
a vendored copy/sample, not the live store.

Two optional addons ship as extras:
- `interactive` -> `prompt_toolkit`, enables `cuhkvoting interactive` (a vim-style TUI).
- `benty` -> `beautifulsoup4`, enables the `cuhkvoting-benty` sync command.

`full` = both.

## Commands

Run everything through `uv` (no global install needed for dev):

```bash
uv run cuhkvoting --help                    # run the CLI
uv run python -m unittest discover -s tests # TESTS (171 tests, unittest-based, no pytest)
```

- Tests are plain `unittest` (no `pytest`, no `conftest.py`). Files are `tests/test_*.py`
  with a `unittest.main()` tail so they also run individually:
  `uv run python -m unittest tests.test_arxiv_retry`.
- `uv run` syncs only the base deps. The optional extras are NOT installed by default, so
  8 interactive tests are skipped. To exercise them: `uv run --extra full python -m unittest
  discover -s tests`.
- Lint with ruff (`uv run ruff check src tests`). There is **no ruff/pyright config** and
  the tree is not currently clean (ruff reports ~128 findings, basedpyright hundreds), so
  neither linter is enforced in CI. Do not try to "fix" the repo-wide noise; match the
  surrounding style instead. Type hints are lightweight and often bare (`dict`, `list`).

Build backend is `uv_build` with a `src/` layout. Entry points (see `pyproject.toml`):
`cuhkvoting = cuhkvoting:main` -> `cli.main()` -> `app()`, and
`cuhkvoting-benty = cuhkvoting.benty:main`.

## Code layout

`src/cuhkvoting/cli.py` is a single ~3600-line module holding essentially all logic. Read
it in sections; it is organised loosely as:

1. Constants + platform dirs (`_user_config_dir`, `_user_cache_dir`, `CONFIG_PATH`, `CACHE_DIR`).
2. `Config` / `RepoConfig` dataclasses and `_load_config()` (TOML via `tomllib`).
3. HTTP + GitHub helpers (`_http_json`, `_github_headers`, `_get_token`, ...).
4. arXiv API (`_arxiv_query`) and INSPIRE fallback (`_inspire_*`).
5. Formatting/parsing (IDs, dates, abstracts, highlights).
6. Cache logic (`_resolve_cache`, `_load_last_list`).
7. `cmd_*` functions (the real command bodies).
8. Typer `app` + `@app.command` thin wrappers, then `main()`.

`interactive.py` (TUI) and `benty.py` (Benty-Fields sync) both **reach into `cli.py`
internals**: they import underscore-prefixed names like `cli._today_list`,
`cli._get_token`, `cli._batch_vote_papers_api`. So the leading-underscore convention is
*not* a hard privacy boundary here; sibling modules and tests use these freely. Keep the
same names when refactoring or you break them.

## Command dispatch pattern (important)

Typer wrappers do **not** contain logic. Each one builds a `SimpleNamespace` of args and
calls a `cmd_*` function through `_run_cmd` -> `_invoke_cmd`:

- `cmd_*` functions take a single `args`-like namespace and return an `int` exit code
  (or raise `typer.Exit`).
- `_invoke_cmd` wraps the call and translates exceptions into friendly stderr messages and
  exit codes. This is the single place that handles `urllib.error.HTTPError`/`URLError`,
  `ConnectionError`, `TimeoutError`, `http.client.IncompleteRead`, `TitleUnresolved`, and
  `SystemExit`.
- Therefore: user-facing errors are conventionally raised as `SystemExit("message")` (e.g.
  from `_run_git` and `_resolve_repo_config`), and `TitleUnresolved` means "arXiv answered
  but the id is absent" (a typo guard), distinct from network errors which propagate
  natively so callers can fall back rather than block.
- New commands: add a `cmd_*` body + a `@app.command` wrapper, and route through `_run_cmd`.
  There is also a nested `admin_app` Typer (added via `app.add_typer(admin_app, name="admin")`).

## List loaders are shared between CLI and TUI

`_today_list`, `_lastweek_list`, `_lastdays_list`, `_topvoted_list`, `_search_list`,
`_show_date_list` each return a `ListData`. They are used by both the plain commands and
`interactive.py`'s `_load_list`, which dispatches tokens via `_dispatch` and **captures
stdout/stderr** those loaders print (via `contextlib.redirect_stdout/stderr`) and turns the
lines into sticky TUI warnings. So: if you add a `typer.echo`/`print` inside a loader, it
will appear as a warning banner in interactive mode, not just on the console.

## Two write paths, always kept in sync

Any operation that mutates the records repo has two implementations:

- **SSH path**: `_has_github_ssh_access()` is checked first; if the SSH banner says
  "successfully authenticated", the code shallow-clones via `_repo_checkout` (context
  manager, auto-cleanup) and commits/pushes with `_run_git` (`_commit_via_ssh` for
  single-file writers; `_batch_vote_papers_ssh` stages many files into one commit).
- **API path**: otherwise, with a token, it uses the GitHub REST/GraphQL API. Batch writes
  go through `_git_batch_commit` (read HEAD -> create tree -> create commit -> PATCH ref,
  retrying once on HTTP 422 for concurrent pushes).

When changing vote/select/sanitize behaviour you must update **both** paths (and the
`_batch_vote_papers_*` pair). Prefer SSH when available; tokens come from
`CUHKVOTING_TOKEN`/`GITHUB_TOKEN`/`GH_TOKEN` or the `gh` CLI.

Only `gravityhub-org/cuhkvoting-records` is accepted; `_resolve_repo_config` hard-errors on
any other `--repo`/`CUHKVOTING_REPO`. Branch defaults to `main` (`CUHKVOTING_BRANCH`).

## Data model / records repo

- `papers/<arxiv_id>.json`: `{id, title, url, abstract, votes: [{user, voted_at}]}`
  plus optional `archived` / `archived_at`. Paper-level timer: if no vote within
  `VOTE_EXPIRY_DAYS` (30), the paper is archived (hidden from topvoted); individual
  votes are never stripped for age. A new vote clears archive and restarts the timer.
  One vote per GitHub username, enforced in CLI.
- `papers/journal_club_records.json`: journal-club selection history (canonical,
  version-stripped ids; de-duplicated by `admin sanitize`).
- `display_names.json`: shared GitHub-username -> display-name table.
- `meta.json`: root, client-maintained high-water-mark of the newest `cuhkvoting` version
  seen, used for passive "please upgrade" notices. Mutated in place (never fully
  replaced). Repair with `cuhkvoting admin set-version`.

Config is generated by `cuhkvoting init-config` at a platform-specific path; if absent all
settings fall back to the defaults hard-coded at the top of `cli.py`. Local cache lives in
`CACHE_DIR`; `last_list.json` there backs `vote <#>` / `show <#>` (the last printed list).
Caches store entries plus a `categories` set; `_resolve_cache` drops removed categories and
fetches only newly-added ones.

## arXiv / INSPIRE behaviour

- arXiv (`export.arxiv.org`) is the primary source. Queries are bounded by
  `ARXIV_RETRY_DELAYS` and `ARXIV_QUERY_MAX_SECONDS`; on exhaustion the original network
  error is re-raised (not `SystemExit`) so INSPIRE/stale-cache fallbacks fire.
- INSPIRE is only a fallback when arXiv is unreachable. "Reachable but empty" stays a hard
  not-found (typo guard) and must not silently fall back.
- These fallback/retry semantics are covered by `tests/test_arxiv_retry.py`,
  `tests/test_inspire_fallback.py`, `tests/test_vote_metadata.py`; change them carefully.

## Testing conventions

Tests mock at the `cuhkvoting.cli.<name>` boundary (e.g.
`mock.patch("cuhkvoting.cli._http_text", ...)`, `mock.patch("cuhkvoting.cli._lookup_local_cache", ...)`).
Interactive TUI tests drive a real `prompt_toolkit` `Application` through a pipe input
(`create_pipe_input`) + `DummyOutput` and assert on session state / rendered fragments
(`tests/test_interactive_smoke.py`). Add tests in this style; do not introduce pytest-only
features.

## Gotchas

- `uv run` **recreates `.venv`** from scratch each time deps change; extras are omitted
  unless you pass `--extra`. If interactive/benty tests are unexpectedly skipped, that is why.
- `.venv`, `uv.lock`, `__pycache__` etc. are gitignored; `uv.lock` is intentionally NOT
  committed.
- Vendor docs live in `docs/` (`interactive.md`, `benty.md`); design specs live in
  `interactive_mode_spec.md` and `userscript_spec.md`. `todo.typ` / `todo.pdf` track the
  roadmap using task tags like `cuv-feat:`, `cuv-fix:`, `cuv-rfctr:` (not git prefixes).
- Commit messages in this repo use a short "Area: summary" style (e.g. `Refactor:`,
  `Vote:`, `Add ...`, `` `sanitize`: ... ``). Match that when committing.
- `voting` (repo root) is an agent-resume helper file, not part of the package.
