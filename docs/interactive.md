# Interactive mode

`cuhkvoting interactive` opens a vim-style, full-screen voting session: browse
the paper list, fold abstracts open, stage papers into a voting list, and cast
all votes at once — no more copying indices between a listing and `vote`.

## Install

The interactive mode needs the optional `prompt_toolkit` dependency, installed
via the `interactive` extra (or `full`, which also includes the Benty addon):

```bash
uv tool install --upgrade "git+https://github.com/gravityhub-org/cuhkvoting.git[interactive]"
# or everything:
uv tool install --upgrade "git+https://github.com/gravityhub-org/cuhkvoting.git[full]"
```

Without the extra, `cuhkvoting interactive` prints an install hint and exits.

## Starting a session

Each form opens the same list its plain counterpart would print:

```bash
cuhkvoting interactive                      # today's papers (default)
cuhkvoting interactive today
cuhkvoting interactive lastweek
cuhkvoting interactive last 3
cuhkvoting interactive topvoted             # all voted papers, no cutoff
cuhkvoting interactive search kilonova
cuhkvoting interactive show 2026-03-12
cuhkvoting interactive 2026-03-12           # bare date  → same as show
cuhkvoting interactive kilonova merger      # bare words → same as search
```

Lists come from the same fetch/cache pipeline as the plain commands, so a warm
cache works offline, and the displayed list is saved as the "last list" — after
quitting, `cuhkvoting vote 3` still refers to paper 3 of what you just browsed.

## The screen

```
| /!\ Note: no papers announced today (UTC); showing last batch (7-15)|  sticky warnings
| ── 2026-07-15 ───────────────────────────────────────────────────── |  day row
| 12. 2507.01234  Gravitational-wave echoes …    [Chan, Li +1]        |
|>13. 2507.02345  Neutron-star mergers …  [Kumar, Sato]  ●           |  cursor row
|      We study the nucleosynthetic yields of binary neutron-star     |  open abstract
| 14. 2507.03456  Dark-matter constraints …      [Okafor +2]          |
| NOR                                                    2 sel  13:34 |  status line
| v add  d del  o open  za abs  / find  :w vote  :q quit  :h help     |  key hints
```

- `●` marks papers staged for voting; `2 sel` counts them.
- `−` marks a staged removal of your vote; `1 del` counts them (status line).
- `✓` marks papers you voted for (green tick and title). The marks are session-local, but in
  `topvoted` earlier votes are recognised on load when your GitHub login is
  available (`CUHKVOTING_USER`/`GITHUB_USER`, or the normal token/credential
  helper chain); otherwise they appear after your first `:w`.
- The current row is one solid inverted bar spanning the full width: the theme's
  foreground becomes the background and vice versa (the `default` theme swaps the
  terminal's own colors), so the same rule works on dark and light themes.
- `13:34` is the cursor position over the list length.
- The mode is `NOR` (normal) or `CMD` while typing a `:`, `/` or `?` command.
- Warnings stay sticky at the top on a yellow background.
- Date-sorted lists (`today`, `lastweek`, `last <#>`, `show <date>`) are split
  into day sections by dim rule rows. While a day's rule is scrolled off the
  top, the day stays pinned at the top of the list; the rule itself is not
  selectable, so paper numbers keep matching `vote <#>`.
- In `topvoted`, tied papers share the plain command's ranking gutter
  (`┳ ┣ ┗`) and the index of non-leading rows is dimmed. While a tie group has
  a row below, wrapped titles and open abstracts keep the `┃` continuation
  line, so the gutter never breaks mid-row.
- The bottom line shows key hints while idle; it becomes the input line as soon
  as you type. Set `key_hints = false` in `[interactive]` to turn the hints off.

## Keys (defaults)

| Key | Action |
|-----|--------|
| `j` / `k`, `↓` / `↑` | move down / up (count prefixes work: `5j`) |
| `ctrl-f` / `ctrl-b`, `PageDown` / `PageUp` | page down / up |
| `gg` / `G` | first / last paper (`<#>G` jumps to paper `<#>`) |
| `v` / `enter` | add the current paper to the voting list |
| `d` / `delete` | unstage it; on a paper you voted for, stage removal of your vote |
| `o` | open the paper's webpage in the browser |
| `za` / `zo` / `zc` | toggle / open / close the abstract |
| `→` / `←` | open / close the current paper's abstract |
| `zR` / `zM` | open / close all abstracts |
| `zi` | follow mode: the selected paper's abstract is always shown open; moving on, papers return to their own fold state |
| `/pat` / `?pat` | search forward / backward (titles, authors, abstracts; wraps) |
| `n` / `N` | next / previous search match |
| `escape` | clear count / leave `:ls` filter / cancel input, help, confirm |

## `:` commands

Commands need `<enter>` to run; while typing, a hint is shown right-aligned.

| Command | Action |
|---------|--------|
| `:<#>` | jump to paper `<#>` |
| `:ls` | show only staged changes (`escape` returns) |
| `:w`, `:write` | review and apply staged votes/removals |
| `:x`, `:wq`, `:write-quit` | apply staged changes, then quit |
| `:q`, `:quit` | quit — refuses while changes are staged |
| `:q!`, `:quit!` | quit and discard staged changes |
| `:theme <name>` | switch color theme (`tab` previews, `enter` saves) |
| `:h`, `:help` | help screen |

`ctrl-c` behaves like `:q`: it never discards staged votes silently.

## Voting

`:w` switches to a confirmation view listing the staged changes. On `y` the
terminal is briefly restored while votes and removals are applied in one batch
(same SSH/API machinery as `cuhkvoting vote`); papers already selected for a
past journal club are skipped with a warning. On failure the staged changes are
kept, so `:w` can simply be retried.

`d` on a paper you have already voted for (green `✓`) stages removal of your
vote instead of unstaging an add: the row gets a red `−` marker and `N del`
appears in the status line. Press `d` again (or `v`) to cancel it. `:w` applies
staged votes and removals together in a single commit; if the removal leaves no
votes and the paper was not selected for a journal club, its record file is
deleted.

Papers whose vote is recorded — including ones `:w` reports as duplicates from
earlier votes — get a green `✓` and a green title for the rest of the session,
so you can keep browsing without losing track of what you already voted for. In
`topvoted`, the rows already carry the voter usernames, so votes you cast in
earlier sessions are ticked on load as well when your login is known.

After quitting, a summary lists this session's changes:

```
Changes this session:
  2507.01234  First title
  2507.05678  Second title  ✗
  2507.09012  Third title  −
    ✗ = duplicated an earlier vote (nothing changed)
    − = vote removed
```

`✗` marks votes that duplicated an earlier vote of yours; `−` marks votes that
were removed; `⊘` marks papers skipped because they were already selected for a
journal club. If the records repo advertises a newer cuhkvoting release, the
upgrade note is appended here (and shown as a sticky warning during the
session).

## Themes

Built-in themes: `default` (your terminal's colors), `onedark`, `gruvbox`,
`catppuccin-mocha`, `solarized-dark`, `nord`.

`:theme ` + `tab` cycles the candidates with a live full-screen preview;
`enter` applies the theme and persists it to `config.toml`; `escape` reverts.

## Configuration

Everything lives in the shared config file (see the README for its location):

```toml
[interactive]
theme = "default"        # one of the built-in theme names
key_hints = true         # show the idle-line key hints
follow = false           # start with zi follow mode already on

[interactive.keys]
# Override any action's keys: a single key, a named key, or a space-separated
# sequence. A string or a list of strings per action.
# Actions: move_down, move_up, page_down, page_up, top, bottom, add, remove,
#          open_url, toggle_abstract, open_abstract, close_abstract, open_all,
#          close_all, toggle_follow, search_fwd, search_bwd, next_match,
#          prev_match, command
add = ["v", "enter"]
top = "g g"              # sequences are space-separated
page_down = "c-f"        # control keys use prompt_toolkit names
```

Invalid overrides are reported as a warning at startup and fall back to the
defaults. The help screen (`:h`) always reflects the effective keymap.

## Current limitations

- `--category`, `--limit`, and `--max-age` are not yet accepted by
  `interactive` subcommands; the plain commands' defaults apply.
- The mouse wheel scrolls the list; once the selection reaches the window edge
  it is dragged along, so the viewport never scrolls past the selection.
- Titles and abstracts reflow to the terminal width and re-wrap when the
  window is resized; the `[display] abstract_wrap` setting only applies to the
  plain commands.
- A pending `g`/`z` prefix is not echoed in the input line (counts are).
