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
| 12. 2507.01234  Gravitational-wave echoes …    [Chan, Li +1]        |
|>13. 2507.02345  Neutron-star mergers …  [Kumar, Sato]  ●           |  cursor row
|      We study the nucleosynthetic yields of binary neutron-star     |  open abstract
| 14. 2507.03456  Dark-matter constraints …      [Okafor +2]          |
| NOR                                                    2 sel  13:34 |  status line
| v add  d del  o open  za abs  / find  :w vote  :q quit  :h help     |  key hints
```

- `●` marks papers staged in the voting list; `2 sel` counts them.
- `13:34` is the cursor position over the list length.
- The mode is `NOR` (normal) or `CMD` while typing a `:`, `/` or `?` command.
- Warnings stay sticky at the top on a yellow background.
- The bottom line shows key hints while idle; it becomes the input line as soon
  as you type. Set `key_hints = false` in `[interactive]` to turn the hints off.

## Keys (defaults)

| Key | Action |
|-----|--------|
| `j` / `k`, `↓` / `↑` | move down / up (count prefixes work: `5j`) |
| `ctrl-f` / `ctrl-b`, `PageDown` / `PageUp` | page down / up |
| `gg` / `G` | first / last paper (`<#>G` jumps to paper `<#>`) |
| `v` / `enter` | add the current paper to the voting list |
| `d` / `delete` | remove it from the voting list |
| `o` | open the paper's webpage in the browser |
| `za` / `zo` / `zc` | toggle / open / close the abstract |
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
| `:ls` | show only the voting list (`escape` returns) |
| `:w`, `:write` | review the voting list and cast the votes |
| `:x`, `:wq`, `:write-quit` | cast the votes, then quit |
| `:q`, `:quit` | quit — refuses if the voting list is not empty |
| `:q!`, `:quit!` | quit and discard the voting list |
| `:theme <name>` | switch color theme (`tab` previews, `enter` saves) |
| `:h`, `:help` | help screen |

`ctrl-c` behaves like `:q`: it never discards staged votes silently.

## Voting

`:w` switches to a confirmation view listing the staged papers. On `y` the
terminal is briefly restored while the votes are cast in one batch (same
SSH/API machinery as `cuhkvoting vote`); papers already selected for a past
journal club are skipped with a warning. On failure the voting list is kept, so
`:w` can simply be retried.

After quitting, a summary lists this session's votes:

```
Votes cast this session:
  2507.01234  First title
  2507.05678  Second title  ✗
    ✗ = duplicated an earlier vote (nothing changed)
```

`✗` marks votes that duplicated an earlier vote of yours; `⊘` marks papers
skipped because they were already selected for a journal club. If the records
repo advertises a newer cuhkvoting release, the upgrade note is appended here
(and shown as a sticky warning during the session).

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
- The mouse wheel scrolls the view, but the next keypress snaps back to the
  cursor.
- A pending `g`/`z` prefix is not echoed in the input line (counts are).
