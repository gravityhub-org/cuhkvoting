# Benty-Fields sync

`cuhkvoting-benty` performs a two-way sync between your Benty-Fields
journal-club page and cuhkvoting records:

- **Benty-Fields → cuhkvoting**: papers you voted for on Benty-Fields are
  voted for in cuhkvoting.
- **cuhkvoting → Benty-Fields**: papers you voted for in cuhkvoting are voted
  for on Benty-Fields.
- Votes explicitly removed in either system are removed in the other. Natural
  6-month expiry in cuhkvoting does not propagate.

## Install

The addon needs the optional `beautifulsoup4` dependency, installed via the
`benty` extra (or `full`, which also includes the interactive mode):

```bash
uv tool install --upgrade "git+https://github.com/gravityhub-org/cuhkvoting.git[benty]"
```

This adds the `cuhkvoting-benty` command.

## Usage

```bash
cuhkvoting-benty                     # fetch + vote for new papers
cuhkvoting-benty --dry-run           # preview without voting
cuhkvoting-benty --no-cache-cookies  # skip cookie persistence
```

## Credentials

Credentials are read from your git credential helper (e.g. libsecret / GNOME
keyring) using `host=benty-fields.com`. If no stored credential is found, you
are prompted interactively.

## Sync tracking and cookies

Already-synced papers are tracked in `benty_synced.json` inside the cache
directory (see [Local cache](../README.md#local-cache)) so they are not voted
on twice.

Session cookies are cached by default to avoid re-logging-in on every run.
Disable this per-run with `--no-cache-cookies`, or permanently via config:

```toml
[benty]
cache_cookies = false
```

## Caveats

If a paper on the Benty-Fields page has no arXiv link, a warning is printed
and the paper is skipped.
