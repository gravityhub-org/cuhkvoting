## cuhkvoting

Minimal CLI to browse arXiv and vote on papers, with paper data and votes stored in GitHub under `papers/`.

### Install

```bash
uv tool install --upgrade git+https://github.com/gravityhub-org/cuhkvoting.git && cuhkvoting --install-completion
```

Or with pip:

```bash
pip install --upgrade git+https://github.com/gravityhub-org/cuhkvoting.git && cuhkvoting --install-completion
```

Optional Benty-Fields addon (adds `cuhkvoting-benty`):

```bash
uv tool install --upgrade "git+https://github.com/gravityhub-org/cuhkvoting.git[benty]"
```


### Quick setup

For read-only commands (`today`, `search`, `topvoted`) you do not need auth.

No GitHub CLI is required.

Auth behavior:

- `today`, `search`, `topvoted`: works without auth
- `vote`: needs write auth via either:
  - token (`CUHKVOTING_TOKEN`, `GITHUB_TOKEN`, or `GH_TOKEN` env var), or
  - a git credential helper (e.g. libsecret / GNOME keyring) - picked up automatically, or
  - git SSH key for `git@github.com`

Set vote identity (optional if global git `user.name` is already set):

```bash
export CUHKVOTING_USER=your-github-username
```

Optional SSH check:

```bash
ssh -T git@github.com
```

Default repo is `gravityhub-org/cuhkvoting-records`.

Optional overrides:

```bash
export CUHKVOTING_REPO=gravityhub-org/cuhkvoting-records
export CUHKVOTING_BRANCH=main
```

### Commands

```bash
# Browse papers
cuhkvoting today
cuhkvoting today "black hole"
cuhkvoting today lensing gravitational waves
cuhkvoting lastweek
cuhkvoting lastweek "black hole"
cuhkvoting lastweek lensing gravitational waves

# Search (uses Inspire HEP API, outputs arXiv entries only, AND semantics)
cuhkvoting search "vision language model"
cuhkvoting search gravitational wave hubble constant --limit 10

# Voting
cuhkvoting topvoted --N 10
cuhkvoting vote 2504.12345
cuhkvoting vote 2504.12345 2504.67890   # vote for multiple papers at once
cuhkvoting vote remove 2504.12345

# Vote by list index (refers to the last printed list from today/lastweek/search/topvoted)
cuhkvoting vote 3
cuhkvoting vote 1 5 2504.12345          # mix of indices and arXiv IDs

# Show full details (title, authors, full abstract) for specific papers
cuhkvoting show 3
cuhkvoting show 1 5 2504.12345          # mix of indices and arXiv IDs

# Journal club records
cuhkvoting record
cuhkvoting select 2504.12345
cuhkvoting admin trash 2504.12345
```

### Category filtering

`today` and `lastweek` filter by arXiv category. The default categories are `gr-qc` and `astro-ph.*`.

Override for a single run (comma-separated or repeatable):

```bash
cuhkvoting lastweek --category hep-th
cuhkvoting lastweek --category "gr-qc,hep-th"
cuhkvoting lastweek --category gr-qc --category hep-th
```

To change the default, set `categories` in the config file (see below).

### Local cache

`today` and `lastweek` cache results locally in `~/.cache/cuhkvoting/` to avoid hitting the arXiv API on every call, and thus avoid exceeding its limit rate.

Default cache lifetime: 60 min for `today`, 360 min for `lastweek`. Running `lastweek` also seeds the `today` cache from its results.

Force a refresh:

```bash
cuhkvoting today --max-age 0
cuhkvoting lastweek --max-age 0
```

Set a custom lifetime (in minutes):

```bash
cuhkvoting today --max-age 30
```

If categories change between runs, the cache is updated automatically: entries for removed categories are dropped locally; entries for added categories are fetched from arXiv and merged in.

### Abstract display

By default no abstract is shown. Use `--abstract` to display abstracts:

```bash
cuhkvoting today --abstract -1    # full abstract
cuhkvoting today --abstract 3     # first 3 wrapped lines
cuhkvoting today --abstract 0     # no abstract (default)
```

### Configuration file

Generate a default config file at `~/.config/cuhkvoting/config.toml`:

```bash
cuhkvoting init-config
```

The file looks like:

```toml
# arXiv categories for today/lastweek queries.
# Supports wildcards, e.g. "astro-ph.*" matches all astro-ph subcategories.
categories = ["gr-qc", "astro-ph.*"]

[cache]
today_max_age = 60      # minutes
lastweek_max_age = 360  # minutes

[display]
# Number of abstract lines to show per entry.
# 0 = none (default), -1 = full abstract, N = first N wrapped lines.
abstract_lines = 0
abstract_wrap = 80      # line wrap width in characters

[vote]
# Show a confirmation prompt when voting by list index (e.g. cuhkvoting vote 3).
confirm_by_number = true
# Human-readable name stored in the shared display_names.json table in the records repo.
# GitHub username is used if empty. Updated automatically on each vote.
display_name = ""

[highlights]
authors = []        # ["Surname, Firstname"]
keywords = []       # regular expressions
keyword_count = -1  # -1 = all, 0 = glyph, N = first N
glyph = "★"
```

If the file is absent, all settings fall back to the defaults shown above.

### Highlights

Mark authors and keywords of interest so they stand out in `today`/`lastweek` listings.

Configure in `~/.config/cuhkvoting/config.toml`:

```toml
[highlights]
# Authors to highlight - "Surname, Firstname" format, case-insensitive.
# Abbreviated arXiv firstnames (e.g. "H. von Helmholtz") are matched
# automatically.
authors = ["von Helmholtz, Hermann", "Einstein, Albert"]

# Keywords as regular expressions - matched against title and abstract.
keywords = ["neutron star", "black hole merger"]

# How many matched keywords to show after each title.
# -1 = all, 0 = glyph only, N = first N (with trailing + if more exist).
keyword_count = -1

# Glyph used when keyword_count = 0.
glyph = "★"
```

Override `keyword_count` per run:

```bash
cuhkvoting today --highlight-keywords 0   # glyph only
cuhkvoting today --highlight-keywords 3   # first 3 matches
cuhkvoting today --highlight-keywords -1  # all matches
```

Matched author lastnames and keyword occurrences in abstracts are colored blue.

### Bash autocomplete

```bash
cuhkvoting --install-completion
```

If you prefer manual setup:

```bash
eval "$(_CUHKVOTING_COMPLETE=bash_source cuhkvoting)"
```

### Benty-Fields sync (optional addon)

`cuhkvoting-benty` performs a two-way sync between your Benty-Fields journal-club page and cuhkvoting records:

- **Benty-Fields → cuhkvoting**: papers you voted for on Benty-Fields are voted for in cuhkvoting.
- **cuhkvoting → Benty-Fields**: papers you voted for in cuhkvoting are voted for on Benty-Fields.
- Votes explicitly removed in either system are removed in the other. Natural 6-month expiry in cuhkvoting does not propagate.

```bash
cuhkvoting-benty           # fetch + vote for new papers
cuhkvoting-benty --dry-run # preview without voting
cuhkvoting-benty --no-cache-cookies  # skip cookie persistence
```

Credentials are read from your git credential helper (e.g. libsecret / GNOME keyring) using `host=benty-fields.com`. If no stored credential is found, you are prompted interactively.

Already-synced papers are tracked in `~/.cache/cuhkvoting/benty_synced.json` so they are not voted on twice.

Session cookies are cached by default to avoid re-logging-in on every run. Disable this per-run with `--no-cache-cookies`, or permanently via config:

```toml
[benty]
cache_cookies = false
```

If a paper on the Benty-Fields page has no arXiv link, a warning is printed and the paper is skipped.

### Data format

Votes and metadata are stored as JSON files in the records repo:

- `papers/<arxiv_id>.json` - one file per paper, one vote per GitHub username enforced by CLI
- `papers/journal_club_records.json` - history of selected papers
- votes expire after 6 months
