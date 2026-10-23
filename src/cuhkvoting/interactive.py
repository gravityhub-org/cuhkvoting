"""Vim-style full-screen voting session (optional addon).

Install with:  pip install "cuhkvoting[interactive]"
Run with:      cuhkvoting interactive [today | lastweek | last <#> | topvoted |
                                       search <kw...> | show <date> | <date> | <keywords>]

The module is split in two halves: the pure logic on top (dispatcher, session
model, command table, keymap, themes) imports cleanly without prompt_toolkit
and is unit-tested directly; the UI wiring below needs prompt_toolkit and is
reached only through `run()`, which the CLI gates on PROMPT_TOOLKIT_OK.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import os
import re
import subprocess
import sys
import urllib.error
import webbrowser
from dataclasses import dataclass, field
from types import SimpleNamespace

import typer

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

try:
    from prompt_toolkit.application import Application, get_app, run_in_terminal
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import ALL_KEYS, KEY_ALIASES
    from prompt_toolkit.layout import (
        ConditionalContainer, Float, FloatContainer, HSplit, Layout, VSplit, Window,
    )
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.mouse_events import MouseEventType
    from prompt_toolkit.styles import DynamicStyle, Style
    PROMPT_TOOLKIT_OK = True
except ImportError:
    PROMPT_TOOLKIT_OK = False

from cuhkvoting import cli

INSTALL_HINT = (
    "Interactive mode needs prompt_toolkit. Install with:\n"
    '  pip install "cuhkvoting[interactive]"'
)

NOR, CMD = "NOR", "CMD"
VOTE_MARK = "●"
VOTED_MARK = "✓"   # voted (or confirmed already-voted) during this session
REMOVAL_MARK = "−"  # staged removal of this paper's vote


# ---------------------------------------------------------------------------
# Config ([interactive] section of the shared config.toml)
# ---------------------------------------------------------------------------

@dataclass
class InteractiveConfig:
    theme: str = "default"
    key_hints: bool = True
    follow: bool = False   # start with zi follow mode already on
    keys: dict = field(default_factory=dict)


def _load_interactive_config() -> InteractiveConfig:
    icfg = InteractiveConfig()
    if cli.CONFIG_PATH.exists():
        try:
            with open(cli.CONFIG_PATH, "rb") as f:
                raw = tomllib.load(f)
        except Exception:
            return icfg
        section = raw.get("interactive", {})
        if isinstance(section, dict):
            icfg.theme = str(section.get("theme", icfg.theme))
            icfg.key_hints = bool(section.get("key_hints", True))
            icfg.follow = bool(section.get("follow", False))
            keys = section.get("keys", {})
            if isinstance(keys, dict):
                icfg.keys = keys
    return icfg


# ---------------------------------------------------------------------------
# Dispatcher: `interactive <tokens>` -> the same list the plain command shows
# ---------------------------------------------------------------------------

def _partition_dates(tokens: list[str]) -> tuple[list, list[str]]:
    date_spans, keywords = [], []
    for tok in tokens:
        span = cli._parse_date_token(tok)
        if span:
            date_spans.append(span)
        else:
            keywords.append(tok)
    return date_spans, keywords


def _dispatch(tokens: list[str]) -> tuple:
    """Map interactive-mode tokens to (loader, args) mirroring the plain CLI."""
    head = tokens[0] if tokens else "today"
    rest = tokens[1:]
    if head == "today":
        return cli._today_list, SimpleNamespace(
            limit=20, keywords=rest or None, max_age=None, category=None)
    if head == "lastweek":
        return cli._lastweek_list, SimpleNamespace(
            limit=1000, keywords=rest or None, max_age=None, category=None)
    if head == "last":
        if not rest or not rest[0].isdigit() or int(rest[0]) < 1:
            raise SystemExit("Usage: cuhkvoting interactive last <#> [keywords]")
        days, keywords = int(rest[0]), rest[1:] or None
        if days == 1:   # last 1 ≡ today (same delegation as cmd_last)
            return cli._today_list, SimpleNamespace(
                limit=1000, keywords=keywords, max_age=None, category=None)
        if days == 7:   # last 7 ≡ lastweek
            return cli._lastweek_list, SimpleNamespace(
                limit=1000, keywords=keywords, max_age=None, category=None)
        return cli._lastdays_list, SimpleNamespace(
            days=days, limit=1000, keywords=keywords, max_age=None, category=None)
    if head == "topvoted":
        return cli._topvoted_list, SimpleNamespace(
            N=None, repo=None, branch=os.getenv("CUHKVOTING_BRANCH", "main"))
    if head == "search":
        return cli._search_list, SimpleNamespace(query=rest, limit=20)
    if head == "show":
        date_spans, keywords = _partition_dates(rest)
        if not date_spans:
            raise SystemExit("Usage: cuhkvoting interactive show <date> [keywords]")
        return cli._show_date_list, SimpleNamespace(
            date_spans=date_spans, keywords=keywords or None, categories=None, limit=200)
    # Bare tokens: dates -> the `show <date>` list, anything else -> `search`.
    date_spans, keywords = _partition_dates(tokens)
    if date_spans:
        return cli._show_date_list, SimpleNamespace(
            date_spans=date_spans, keywords=keywords or None, categories=None, limit=200)
    return cli._search_list, SimpleNamespace(query=tokens, limit=20)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _load_list(tokens: list[str], cfg) -> tuple:
    """Run the dispatched loader; capture warnings that cli helpers print directly.

    `_resolve_cache` (stale-cache notes) may write straight to stdout/stderr;
    inside the TUI those lines must become sticky warnings instead of corrupting
    the screen. Returns (ListData, captured lines, loader) — the loader lets
    `run` tell date-sorted lists from relevance-sorted ones for day grouping.
    """
    loader, args = _dispatch(tokens)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        # _search_list is the one loader that does not take the config.
        data = loader(args) if loader is cli._search_list else loader(args, cfg)
    captured = [
        _strip_ansi(line)
        for line in (out.getvalue() + err.getvalue()).splitlines()
        if line.strip()
    ]
    return data, captured, loader


# ---------------------------------------------------------------------------
# Session model (pure)
# ---------------------------------------------------------------------------

@dataclass
class Paper:
    index: int          # 1-based position in the full list; matches `vote <#>`
    entry: dict
    open: bool = False  # abstract fold state


class Session:
    """All TUI state that is independent of prompt_toolkit."""

    def __init__(self, entries: list[dict], theme: str = "default") -> None:
        self.papers = [Paper(i, e) for i, e in enumerate(entries, 1)]
        self.cursor = 0                        # position within visible()
        self.voting: list[str] = []            # staged arXiv ids, in staging order
        self.unvoting: list[str] = []          # staged vote removals, in staging order
        self.voted: set[str] = set()           # cast this session (duplicates included)
        self.ls_only = False                   # :ls filter active
        self.group_by_day = False              # date rows + sticky header (date-sorted lists)
        self.follow = False                    # zi: selected abstract always open
        self.view = "list"                     # list | help | confirm
        self.mode = NOR
        self.pending_count = ""
        self.feedback = ""
        self.warnings: list[str] = []
        self.search_pat = ""
        self.search_dir = 1
        self.session_votes: list[dict] = []    # {"id","title","status"} rows
        self.exit_after_write = False
        self.theme = theme
        self.outdated_msg: str | None = None

    # -- view helpers ------------------------------------------------------

    def visible(self) -> list[Paper]:
        if self.ls_only:
            staged = set(self.voting) | set(self.unvoting)
            return [p for p in self.papers if str(p.entry.get("id", "")) in staged]
        return self.papers

    def current(self) -> Paper | None:
        vis = self.visible()
        return vis[self.cursor] if 0 <= self.cursor < len(vis) else None

    def title_of(self, paper_id: str) -> str:
        for p in self.papers:
            if str(p.entry.get("id", "")) == paper_id:
                return " ".join(str(p.entry.get("title", "")).split())
        return ""

    # -- movement ----------------------------------------------------------

    def move(self, n: int) -> None:
        vis = self.visible()
        if vis:
            self.cursor = max(0, min(len(vis) - 1, self.cursor + n))

    def jump_first(self) -> None:
        self.cursor = 0

    def jump_last(self) -> None:
        self.cursor = max(0, len(self.visible()) - 1)

    def jump_index(self, index: int) -> None:
        """Jump to the paper whose full-list index is `index` (1-based)."""
        for pos, paper in enumerate(self.visible()):
            if paper.index == index:
                self.cursor = pos
                return
        self.feedback = f"No paper {index} in the current list"

    def take_count(self) -> int:
        n = int(self.pending_count) if self.pending_count else 1
        self.pending_count = ""
        return n

    # -- voting list -------------------------------------------------------

    def _after_unstage(self, message: str) -> None:
        """Re-clamp the :ls view after a staged row vanished (or leave it when empty)."""
        self.feedback = message
        if not self.ls_only:
            return
        if self.voting or self.unvoting:
            self.move(0)  # re-clamp: the row under the cursor vanished
        else:
            self.exit_ls()
            self.feedback = f"{message} — nothing staged"

    def add_current(self) -> None:
        p = self.current()
        if p is None:
            return
        pid = str(p.entry.get("id", ""))
        if pid in self.voting:
            self.feedback = f"{pid} is already in the voting list"
        elif pid in self.unvoting:
            self.unvoting.remove(pid)
            self._after_unstage(f"Cancelled removal of {pid}")
        elif pid in self.voted:
            self.feedback = f"You have already voted for {pid}"
        else:
            self.voting.append(pid)
            self.feedback = f"Added {pid} to the voting list"

    def remove_current(self) -> None:
        p = self.current()
        if p is None:
            return
        pid = str(p.entry.get("id", ""))
        if pid in self.voting:
            self.voting.remove(pid)
            self._after_unstage(f"Removed {pid} from the voting list")
            return
        if pid in self.voted:
            if pid in self.unvoting:
                self.unvoting.remove(pid)
                self._after_unstage(f"Cancelled removal of {pid}")
            else:
                self.unvoting.append(pid)
                self.feedback = f"Staged removal of your vote for {pid}"
            return
        self.feedback = f"{pid} is not in the voting list (and you have not voted for it)"

    def enter_ls(self) -> None:
        if not self.voting and not self.unvoting:
            self.feedback = "Nothing staged"
            return
        cur = self.current()
        self.ls_only = True
        self.cursor = 0
        staged = set(self.voting) | set(self.unvoting)
        if cur is not None and str(cur.entry.get("id", "")) in staged:
            self.jump_index(cur.index)

    def exit_ls(self) -> None:
        if not self.ls_only:
            return
        cur = self.current()
        self.ls_only = False
        if cur is not None:
            self.jump_index(cur.index)
        else:
            self.cursor = 0

    # -- abstract folds ----------------------------------------------------

    def toggle_abstract(self) -> None:
        p = self.current()
        if p is not None:
            p.open = not p.open

    def open_abstract(self) -> None:
        p = self.current()
        if p is not None:
            p.open = True  # spec: does nothing if already open

    def close_abstract(self) -> None:
        p = self.current()
        if p is not None:
            p.open = False

    def open_all(self) -> None:
        for p in self.papers:
            p.open = True

    def close_all(self) -> None:
        for p in self.papers:
            p.open = False

    def toggle_follow(self) -> None:
        self.follow = not self.follow
        self.feedback = (
            "Follow mode: the selected abstract stays open"
            if self.follow else "Follow mode off"
        )

    def abstract_open(self, paper: Paper) -> bool:
        """Effective fold state: follow mode overlays 'open' onto the current paper.

        A view-level overlay only — `paper.open` is never touched, so moving away
        returns each paper to its own fold state.
        """
        return paper.open or (self.follow and paper is self.current())

    # -- search ------------------------------------------------------------

    def _haystack(self, paper: Paper) -> str:
        e = paper.entry
        authors = e.get("authors", []) or []
        return " ".join(
            [str(e.get("title", "")), " ".join(authors), str(e.get("abstract", ""))]
        ).lower()

    def search(self, pattern: str, direction: int) -> None:
        pattern = pattern.strip()
        if not pattern:
            return
        self.search_pat = pattern
        self.search_dir = direction
        self.next_match()

    def next_match(self, reverse: bool = False) -> None:
        if not self.search_pat:
            self.feedback = "No previous search"
            return
        vis = self.visible()
        if not vis:
            return
        pat = self.search_pat.lower()
        matches = [i for i, p in enumerate(vis) if pat in self._haystack(p)]
        if not matches:
            self.feedback = f"Pattern not found: {self.search_pat}"
            return
        step = self.search_dir * (-1 if reverse else 1)
        n = len(vis)
        for k in range(1, n + 1):  # wraps; lands back on the cursor if unique
            i = (self.cursor + step * k) % n
            if pat in self._haystack(vis[i]):
                self.cursor = i
                self.feedback = f"match {matches.index(i) + 1}/{len(matches)}"
                return

    # -- status line -------------------------------------------------------

    def status_right(self) -> str:
        vis = self.visible()
        pos = self.cursor + 1 if vis else 0
        removals = f"  {len(self.unvoting)} del" if self.unvoting else ""
        return f"{len(self.voting)} sel{removals}  {pos}:{len(vis)}"


# ---------------------------------------------------------------------------
# `:` commands
# ---------------------------------------------------------------------------

COMMANDS: dict[str, tuple[tuple[str, ...], str]] = {
    # canonical: (aliases, hint shown right-aligned while the command is typed)
    "quit":       (("q",),      "Press <enter> to quit (refuses if the voting list is not empty)"),
    "quit!":      (("q!",),     "Press <enter> to quit and discard the voting list"),
    "write":      (("w",),      "Press <enter> to review and cast your votes"),
    "write-quit": (("wq", "x"), "Press <enter> to cast your votes and quit"),
    "ls":         ((),          "Press <enter> to show only the voting list (<escape> returns)"),
    "help":       (("h",),      "Press <enter> to display the help"),
    "theme":      ((),          "theme <name> — <tab> previews, <enter> saves to config.toml"),
}


def _canonical_command(word: str) -> str | None:
    for name, (aliases, _hint) in COMMANDS.items():
        if word == name or word in aliases:
            return name
    return None


def _parse_command(text: str):
    """':' input -> ("jump", <#>) | (canonical_name, args) | None if unknown."""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return ("jump", int(text))
    parts = text.split()
    name = _canonical_command(parts[0])
    return (name, parts[1:]) if name else None


def _command_hint(prefix: str, text: str) -> str:
    """Right-aligned hint for the input line while a command is being typed."""
    if prefix == "/":
        return "Press <enter> to search forward"
    if prefix == "?":
        return "Press <enter> to search backward"
    text = text.strip()
    if not text:
        return ""
    if text.isdigit():
        return f"Press <enter> to jump to paper {text}"
    word = text.split()[0]
    if word == "theme":
        arg = text[len("theme"):].strip()
        names = [t for t in THEMES if t.startswith(arg)]
        return ", ".join(names) if names else "No such theme"
    name = _canonical_command(word)
    if name:
        return COMMANDS[name][1]
    prefixes = [n for n, (aliases, _h) in COMMANDS.items()
                if n.startswith(text) or any(a.startswith(text) for a in aliases)]
    return ", ".join(f":{p}" for p in prefixes) if prefixes else ""


# ---------------------------------------------------------------------------
# Keymap
# ---------------------------------------------------------------------------

DEFAULT_KEYS: dict[str, list[str]] = {
    "move_down":       ["j", "down"],
    "move_up":         ["k", "up"],
    "page_down":       ["c-f", "pagedown"],
    "page_up":         ["c-b", "pageup"],
    "top":             ["g g"],
    "bottom":          ["G"],
    "add":             ["v", "enter"],
    "remove":          ["d", "delete"],
    "open_url":        ["o"],
    "toggle_abstract": ["z a"],
    "open_abstract":   ["z o", "right"],
    "close_abstract":  ["z c", "left"],
    "open_all":        ["z R"],
    "close_all":       ["z M"],
    "toggle_follow":   ["z i"],
    "search_fwd":      ["/"],
    "search_bwd":      ["?"],
    "next_match":      ["n"],
    "prev_match":      ["N"],
    "command":         [":"],
}

# (action or literal ":cmd", short label) for the idle-line hint strip
HINT_ITEMS: list[tuple[str, str]] = [
    ("add", "add"),
    ("remove", "del"),
    ("open_url", "open"),
    ("toggle_abstract", "abs"),
    ("search_fwd", "find"),
    (":w", "vote"),
    (":q", "quit"),
    (":h", "help"),
]


def _key_tokens(spec: object) -> tuple[str, ...] | None:
    """'g g' -> ('g', 'g'); None when the spec is not a usable key sequence."""
    if not isinstance(spec, str) or not spec.strip():
        return None
    tokens = tuple(spec.split())
    for tok in tokens:
        if len(tok) == 1:
            continue
        # Multi-char tokens must be key names prompt_toolkit knows ("c-f",
        # "pagedown") or aliases of them ("enter", "tab").
        if PROMPT_TOOLKIT_OK and tok not in ALL_KEYS and tok not in KEY_ALIASES:
            return None
    return tokens


def _effective_keymap(overrides: dict) -> tuple[dict[str, list[str]], list[str]]:
    """DEFAULT_KEYS with [interactive.keys] applied; invalid entries keep the default."""
    keymap = {action: list(keys) for action, keys in DEFAULT_KEYS.items()}
    problems: list[str] = []
    for action, spec in overrides.items():
        if action not in DEFAULT_KEYS:
            problems.append(f"[interactive.keys] unknown action '{action}' (ignored)")
            continue
        specs = spec if isinstance(spec, list) else [spec]
        parsed = [_key_tokens(s) for s in specs]
        if not parsed or any(p is None for p in parsed):
            problems.append(f"[interactive.keys] invalid key for '{action}' (keeping default)")
            continue
        keymap[action] = [" ".join(p) for p in parsed]
    return keymap, problems


def _key_display(spec: str) -> str:
    return spec.replace(" ", "")


def _hint_strip(keymap: dict[str, list[str]], width: int) -> str:
    """Compact key hints for the idle input line, truncated whole-item-wise."""
    items = []
    for action, label in HINT_ITEMS:
        key = action if action.startswith(":") else _key_display(keymap[action][0])
        items.append(f"{key} {label}")
    out = ""
    for item in items:
        candidate = item if not out else f"{out}  {item}"
        if len(candidate) > width:
            break
        out = candidate
    return out


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------

THEMES: dict[str, dict[str, str]] = {
    # Terminal-native palette, matching the plain CLI's colors.
    "default": {
        "body":      "",
        "status":    "reverse",
        "warning":   "bg:ansiyellow ansiblack",
        "cursor":    "reverse",
        "index":     "",
        "index.dim": "ansibrightblack",
        "id":        "ansibrightblue",
        "title":     "",
        "authors":   "",
        "author.hl": "ansibrightblue bold",
        "kw":        "ansibrightblue bold",
        "mark":      "ansigreen bold",
        "voted":     "ansigreen",
        "removal":   "ansired",
        "search":    "underline bold",
        "day":       "ansibrightblack",
        "hint":      "ansibrightblack",
        "feedback":  "italic",
        "abstract":  "",
        "voters":    "ansicyan",
    },
    "onedark": {
        "body":      "bg:#282c34 #abb2bf",
        "status":    "bg:#3e4451 #abb2bf",
        "warning":   "bg:#e5c07b #282c34",
        "cursor":    "bg:#3e4451 bold",
        "index":     "#5c6370",
        "index.dim": "#4b5263",
        "id":        "#61afef",
        "title":     "#abb2bf",
        "authors":   "#5c6370",
        "author.hl": "#c678dd bold",
        "kw":        "#e5c07b bold",
        "mark":      "#98c379 bold",
        "voted":     "#98c379",
        "removal":   "#e06c75",
        "search":    "bg:#61afef #282c34",
        "day":       "#5c6370 bold",
        "hint":      "#5c6370",
        "feedback":  "#56b6c2 italic",
        "abstract":  "#848b98",
        "voters":    "#56b6c2",
    },
    "gruvbox": {
        "body":      "bg:#282828 #ebdbb2",
        "status":    "bg:#504945 #ebdbb2",
        "warning":   "bg:#fabd2f #282828",
        "cursor":    "bg:#504945 bold",
        "index":     "#928374",
        "index.dim": "#665c54",
        "id":        "#83a598",
        "title":     "#ebdbb2",
        "authors":   "#928374",
        "author.hl": "#d3869b bold",
        "kw":        "#fabd2f bold",
        "mark":      "#b8bb26 bold",
        "voted":     "#b8bb26",
        "removal":   "#fb4934",
        "search":    "bg:#fe8019 #282828",
        "day":       "#928374 bold",
        "hint":      "#928374",
        "feedback":  "#8ec07c italic",
        "abstract":  "#a89984",
        "voters":    "#8ec07c",
    },
    "catppuccin-mocha": {
        "body":      "bg:#1e1e2e #cdd6f4",
        "status":    "bg:#313244 #cdd6f4",
        "warning":   "bg:#f9e2af #1e1e2e",
        "cursor":    "bg:#45475a bold",
        "index":     "#6c7086",
        "index.dim": "#585b70",
        "id":        "#89b4fa",
        "title":     "#cdd6f4",
        "authors":   "#6c7086",
        "author.hl": "#cba6f7 bold",
        "kw":        "#f9e2af bold",
        "mark":      "#a6e3a1 bold",
        "voted":     "#a6e3a1",
        "removal":   "#f38ba8",
        "search":    "bg:#89b4fa #1e1e2e",
        "day":       "#6c7086 bold",
        "hint":      "#6c7086",
        "feedback":  "#94e2d5 italic",
        "abstract":  "#9399b2",
        "voters":    "#94e2d5",
    },
    "solarized-dark": {
        "body":      "bg:#002b36 #839496",
        "status":    "bg:#073642 #93a1a1",
        "warning":   "bg:#b58900 #002b36",
        "cursor":    "bg:#073642 bold",
        "index":     "#586e75",
        "index.dim": "#465a61",
        "id":        "#268bd2",
        "title":     "#93a1a1",
        "authors":   "#586e75",
        "author.hl": "#6c71c4 bold",
        "kw":        "#b58900 bold",
        "mark":      "#859900 bold",
        "voted":     "#859900",
        "removal":   "#dc322f",
        "search":    "bg:#268bd2 #002b36",
        "day":       "#586e75 bold",
        "hint":      "#586e75",
        "feedback":  "#2aa198 italic",
        "abstract":  "#657b83",
        "voters":    "#2aa198",
    },
    "nord": {
        "body":      "bg:#2e3440 #d8dee9",
        "status":    "bg:#3b4252 #d8dee9",
        "warning":   "bg:#ebcb8b #2e3440",
        "cursor":    "bg:#434c5e bold",
        "index":     "#4c566a",
        "index.dim": "#434c5e",
        "id":        "#88c0d0",
        "title":     "#d8dee9",
        "authors":   "#4c566a",
        "author.hl": "#b48ead bold",
        "kw":        "#ebcb8b bold",
        "mark":      "#a3be8c bold",
        "voted":     "#a3be8c",
        "removal":   "#bf616a",
        "search":    "bg:#88c0d0 #2e3440",
        "day":       "#4c566a bold",
        "hint":      "#4c566a",
        "feedback":  "#8fbcbb italic",
        "abstract":  "#81a1c1",
        "voters":    "#8fbcbb",
    },
}


def _theme_style(name: str):
    return Style.from_dict(THEMES.get(name, THEMES["default"]))


def _set_theme_in_toml(text: str, name: str) -> str:
    """Set `theme = "<name>"` inside [interactive], editing the TOML as text.

    stdlib tomllib cannot write TOML; a targeted line edit keeps every other
    section, comment, and [interactive.keys] untouched.
    """
    theme_line = f'theme = "{name}"'
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == "[interactive]"), None)
    if start is None:
        base = text if not text or text.endswith("\n") else text + "\n"
        sep = "\n" if base.strip() else ""
        return f"{base}{sep}[interactive]\n{theme_line}\n"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )
    for i in range(start + 1, end):
        uncommented = lines[i].split("#", 1)[0]
        if uncommented.split("=", 1)[0].strip() == "theme":
            lines[i] = theme_line
            break
    else:
        lines.insert(start + 1, theme_line)
    return "\n".join(lines) + "\n"


def _persist_theme(name: str) -> str | None:
    """Write the theme to config.toml; error string (for the feedback line) on failure."""
    try:
        text = cli.CONFIG_PATH.read_text(encoding="utf-8") if cli.CONFIG_PATH.exists() else ""
        cli.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cli.CONFIG_PATH.write_text(_set_theme_in_toml(text, name), encoding="utf-8")
        return None
    except OSError as exc:
        return f"Could not save theme: {exc}"


# ---------------------------------------------------------------------------
# Fragment rendering (style classes; the active theme gives them colors)
# ---------------------------------------------------------------------------

def _match_spans(text: str, keywords: list[str], search_pat: str) -> list[tuple[int, int, str]]:
    """Non-overlapping (start, end, class) spans for keyword and search hits."""
    spans: list[tuple[int, int, str]] = []
    for kw in keywords:
        rx = cli._get_re(kw)
        if rx is None:
            continue
        spans.extend((m.start(), m.end(), "class:kw") for m in rx.finditer(text))
    if search_pat:
        low, pat = text.lower(), search_pat.lower()
        i = low.find(pat)
        while i >= 0:
            spans.append((i, i + len(pat), "class:search"))
            i = low.find(pat, i + len(pat))
    spans.sort()
    kept: list[tuple[int, int, str]] = []
    for span in spans:
        if not kept or span[0] >= kept[-1][1]:
            kept.append(span)
    return kept


def _styled_text(text: str, base: str, keywords: list[str], search_pat: str) -> list:
    """(style, text) fragments for `text`, marking keyword/search spans."""
    spans = _match_spans(text, keywords, search_pat)
    if not spans:
        return [(base, text)]
    frags, pos = [], 0
    for start, end, cls in spans:
        if start > pos:
            frags.append((base, text[pos:start]))
        frags.append((f"{base} {cls}".strip(), text[start:end]))
        pos = end
    if pos < len(text):
        frags.append((base, text[pos:]))
    return frags


def _emit_run(out: list, style: str, text: str) -> None:
    """Append (style, text) to `out`, merging with the previous run when equal."""
    if not text:
        return
    if out and out[-1][0] == style:
        out[-1] = (style, out[-1][1] + text)
    else:
        out.append((style, text))


def _wrap_styled(frags: list, width: int, indent: str = "",
                 indent_style: str = "", *, indent_first: bool = False) -> list:
    """Reflow one paragraph of styled fragments to fit `width` columns.

    Words break at spaces; space runs are preserved when they stay on a line;
    words longer than a line are hard-broken. Styles survive the re-flow.
    Continuation lines start with `indent`, styled with `indent_style` — and so
    does the first line when `indent_first` is set (abstracts indent fully).
    Every produced line ends with "\\n".
    """
    width = max(1, int(width))
    if len(indent) >= width:
        indent, indent_style = "", ""

    # (char, style) stream — every character, spaces included.
    chars: list[tuple[str, str]] = [
        (ch, style) for style, text in frags for ch in text
    ]

    # Leading spaces belong to the first line; other runs separate words.
    lead: list[tuple[str, str]] = []
    i = 0
    while i < len(chars) and chars[i][0].isspace():
        lead.append(chars[i])
        i += 1
    words: list[tuple[list[tuple[str, str]], list[tuple[str, str]]]] = []
    while i < len(chars):
        j = i
        while j < len(chars) and not chars[j][0].isspace():
            j += 1
        k = j
        while k < len(chars) and chars[k][0].isspace():
            k += 1
        words.append((chars[i:j], chars[j:k]))
        i = k
    if not words:
        return []

    out: list = []
    buf: list[tuple[str, str]] = list(lead)   # current line (char, style), sans indent
    used = len(buf) + (len(indent) if indent_first else 0)
    first = True
    has_word = False

    def flush() -> None:
        nonlocal buf, used, first, has_word
        _emit_run(out, indent_style, indent if (indent_first or not first) else "")
        for ch, style in buf:
            _emit_run(out, style, ch)
        _emit_run(out, "", "\n")
        buf, used, has_word = [], len(indent), False
        first = False

    for idx, (word, _space) in enumerate(words):
        sep = words[idx - 1][1] if (idx > 0 and has_word) else []
        fresh = width - len(indent)      # capacity of any continuation line
        # Move the whole word to a fresh line when it does not fit here but
        # would fit there; otherwise fall through and hard-break it in place.
        if has_word and len(word) <= fresh and used + len(sep) + len(word) > width:
            flush()
            sep = []
        rest = list(word)
        while rest:
            if has_word and sep:
                if used + len(sep) + 1 <= width:
                    buf.extend(sep)
                    used += len(sep)
                    sep = []
                else:
                    flush()  # no room for the space run: wrap before the word
                    continue
            cap = width - used
            if cap <= 0:
                flush()
                continue
            take = min(len(rest), cap)
            buf.extend(rest[:take])
            used += take
            rest = rest[take:]
            has_word = True
            if rest:
                flush()
    flush()
    return out


def _rank_roles(papers: list[Paper]) -> tuple[bool, dict[int, str]]:
    """Tie-gutter roles for ranked (topvoted) rows: (has_ties, {index: role}).

    Roles are single / top / mid / bottom, mirroring the plain command's
    grouping; (False, {}) means "render plain rows" (not a ranked list, or no
    ties among the visible papers).
    """
    if not papers or any("votes" not in p.entry for p in papers):
        return False, {}
    groups: list[list[Paper]] = []
    for paper in papers:
        votes = int(paper.entry.get("votes", 0))
        if groups and int(groups[-1][0].entry.get("votes", 0)) == votes:
            groups[-1].append(paper)
        else:
            groups.append([paper])
    if not any(len(g) > 1 for g in groups):
        return False, {}
    roles: dict[int, str] = {}
    for group in groups:
        for pos, paper in enumerate(group):
            if len(group) == 1:
                roles[paper.index] = "single"
            elif pos == 0:
                roles[paper.index] = "top"
            elif pos == len(group) - 1:
                roles[paper.index] = "bottom"
            else:
                roles[paper.index] = "mid"
    return True, roles


def _day_of(entry: dict) -> str:
    """Day granularity of an entry ("YYYY-MM-DD"); "" when there is none."""
    return str(entry.get("published", ""))[:10]


def _display_items(session: Session) -> list[tuple[str, object]]:
    """Visible list as display rows: ("day", "YYYY-MM-DD") and ("paper", Paper).

    Day rows appear only for date-sorted lists (``session.group_by_day``) that
    span more than one day; papers without a date inherit the previous day.
    """
    papers = session.visible()
    if session.group_by_day:
        days = {_day_of(p.entry) for p in papers if _day_of(p.entry)}
        if len(days) > 1:
            items: list[tuple[str, object]] = []
            prev = ""
            for paper in papers:
                day = _day_of(paper.entry)
                if day and day != prev:
                    items.append(("day", day))
                    prev = day
                items.append(("paper", paper))
            return items
    return [("paper", p) for p in papers]


def _day_rule(day: str, width: int) -> str:
    """Full-width dim rule introducing a day's section, e.g. ``── 2026-10-15 ───``."""
    label = f"── {day} "
    return label + "─" * max(0, width - len(label) - 1) + "\n"


def _entry_fragments(paper: Paper, session: Session, cfg, width: int,
                     role: str | None = None) -> list:
    """Fragments for one paper, reflowed to `width`; every line ends with '\\n'.

    `role` is a tie-gutter position (single/top/mid/bottom) set for topvoted
    rows when the visible list has ties; None renders a plain row.
    """
    e = paper.entry
    is_cur = paper is session.current()
    pid = str(e.get("id", ""))
    sel = " class:cursor" if is_cur else ""
    vtd = " class:voted" if pid in session.voted else ""
    keywords = cfg.highlight_keywords or []
    title = " ".join(str(e.get("title", "")).split())

    dim = role in ("mid", "bottom")
    idx_cls = f"class:index.dim{sel}" if dim else f"class:index{sel}"
    row: list = [(idx_cls, f"{paper.index:>3}. ")]
    if role is not None:
        box = {"single": " ", "top": "┳", "mid": "┣", "bottom": "┗"}[role]
        row.append((sel, f"{box} "))
    row.append((f"class:id{sel}", pid))
    row.append((sel, "  "))
    # Only the title is tinted for voted papers; the rest of the row keeps its
    # normal colors (the green tick at the end stays as the marker).
    row.extend(_styled_text(title, f"class:title{sel}{vtd}", [], session.search_pat))

    authors = e.get("authors", []) or []
    if authors:
        row.append((f"class:authors{sel}", "  ["))
        shown = authors[:3]
        for i, author in enumerate(shown):
            lastname = cli._last_name(author)
            highlighted = any(cli._author_matches_highlight(author, h)
                              for h in cfg.highlight_authors)
            cls = f"class:author.hl{sel}" if highlighted else f"class:authors{sel}"
            row.append((cls, lastname))
            if i < len(shown) - 1:
                row.append((f"class:authors{sel}", ", "))
        row.append((f"class:authors{sel}", "]"))

    if keywords:
        matches = cli._find_keyword_matches([title, str(e.get("abstract", ""))], keywords)
        if matches:
            row.append((f"class:kw{sel}", f"  {cfg.highlight_glyph} {', '.join(matches)}"))

    if "voters" in e:  # topvoted rows carry votes/voters instead of authors
        n = int(e.get("votes", 0))
        row.append((f"class:voters{sel}", f"  [{e['voters']}] ⇒ {n} vote{'s' if n != 1 else ''}"))

    if pid in session.voting:
        row.append((f"class:mark{sel}", f"  {VOTE_MARK}"))
    if pid in session.voted:
        row.append((f"class:voted{sel}", f"  {VOTED_MARK}"))
    if pid in session.unvoting:
        row.append((f"class:removal{sel}", f"  {REMOVAL_MARK}"))

    # Wrapped rows re-align under the title; tie members that still have a row
    # below keep the ┃ continuation in the box column so the gutter stays
    # unbroken (the last member has nothing below, mirroring plain `topvoted`).
    hang = len(f"{paper.index:>3}. ") + (2 if role is not None else 0) + len(pid) + 2
    if role in ("top", "mid"):
        indent = "     ┃ " + " " * max(0, hang - 7)
    else:
        indent = " " * hang
    frags = _wrap_styled(row, width, indent=indent, indent_style=sel)

    if session.abstract_open(paper):
        abstract = " ".join(str(e.get("abstract", "")).split())
        if abstract:
            if role in ("top", "mid"):
                indent = "     ┃ "        # mirror the plain command's ┃ continuation
            elif role in ("bottom", "single"):
                indent = "       "
            else:
                indent = "      "
            body = _styled_text(abstract, "class:abstract", keywords, session.search_pat)
            frags.extend(_wrap_styled(
                body, width, indent=indent,
                indent_style="class:abstract", indent_first=True,
            ))
        else:
            frags.append(("class:abstract", "(no abstract available)\n"))
    return frags


def _help_lines(keymap: dict[str, list[str]]) -> list[tuple[str, str]]:
    """(key column, description) rows for the help view, from the effective keymap."""
    def k(action: str) -> str:
        return " / ".join(_key_display(s) for s in keymap[action])

    return [
        ("Movement", ""),
        (f"{k('move_down')}, {k('move_up')}", "move down / up (count prefixes work: 5j)"),
        (f"{k('page_down')}, {k('page_up')}", "page down / up"),
        (f"{k('top')}, {k('bottom')}", "first / last paper (<#>gg / <#>G jump to paper <#>)"),
        (":<#>", "jump to paper <#>"),
        ("", ""),
        ("Staged changes", ""),
        (k("add"), "vote for the current paper"),
        (k("remove"), "unstage it, or stage removal of your vote"),
        (":ls", "show only staged changes (<escape> returns)"),
        ("", ""),
        ("Consulting", ""),
        (k("open_url"), "open the paper's webpage in the browser"),
        (f"{k('toggle_abstract')} / {k('open_abstract')} / {k('close_abstract')}",
         "toggle / open / close the abstract"),
        (f"{k('open_all')} / {k('close_all')}", "open / close all abstracts"),
        (k("toggle_follow"), "follow mode: keep the selected paper's abstract open"),
        (f"{k('search_fwd')}<pat>, {k('search_bwd')}<pat>",
         "search forward / backward (titles, authors, abstracts; wraps)"),
        (f"{k('next_match')} / {k('prev_match')}", "next / previous match"),
        ("", ""),
        ("Voting and leaving", ""),
        (":w, :write", "review and apply staged changes"),
        (":x, :wq, :write-quit", "apply staged changes, then quit"),
        (":q, :quit", "quit (refuses while changes are staged)"),
        (":q!, :quit!", "quit and discard staged changes"),
        ("", ""),
        ("Other", ""),
        (":theme <name>", "color theme (<tab> previews, <enter> saves to config.toml)"),
        (":h, :help", "this help"),
        ("<escape>", "back / cancel"),
    ]


def _help_fragments(keymap: dict[str, list[str]]) -> list:
    frags: list = [("bold", "cuhkvoting interactive — commands\n\n")]
    for key_col, desc in _help_lines(keymap):
        if not desc and key_col:  # section header
            frags.append(("bold underline", f"{key_col}\n"))
        elif key_col:
            frags.append(("class:id", f"  {key_col:<26}"))
            frags.append(("", f"{desc}\n"))
        else:
            frags.append(("", "\n"))
    frags.append(("class:hint", "\nPress <escape> to return.\n"))
    return frags


def _confirm_fragments(session: Session) -> list:
    frags: list = []
    if session.voting:
        frags.append(("bold", "You are about to vote for:\n\n"))
        width = len(str(len(session.voting)))
        for i, pid in enumerate(session.voting, 1):
            frags.append(("", f"  {i:>{width}}. "))
            frags.append(("class:id", pid))
            frags.append(("class:title", f"  {session.title_of(pid)}\n"))
    if session.unvoting:
        if frags:
            frags.append(("", "\n"))
        frags.append(("bold", "You are about to remove your vote for:\n\n"))
        width = len(str(len(session.unvoting)))
        for i, pid in enumerate(session.unvoting, 1):
            frags.append(("", f"  {i:>{width}}. "))
            frags.append(("class:id", pid))
            frags.append(("class:title", f"  {session.title_of(pid)}\n"))
    adds, removals = len(session.voting), len(session.unvoting)
    if not removals:
        prompt = f"Cast {adds} vote{'s' if adds != 1 else ''}? "
    elif not adds:
        prompt = f"Remove {removals} vote{'s' if removals != 1 else ''}? "
    else:
        prompt = f"Apply {adds + removals} changes? "
    frags.append(("bold", f"\n{prompt}"))
    frags.append(("class:hint", "[y]es / [n]o\n"))
    return frags


def _warning_fragments(session: Session) -> list:
    frags: list = []
    for warning in session.warnings:
        for i, line in enumerate(warning.splitlines() or [""]):
            prefix = "/!\\ " if i == 0 else "    "
            frags.append(("class:warning", f"{prefix}{line}\n"))
    return frags


# ---------------------------------------------------------------------------
# Vote casting (runs with the terminal restored, via run_in_terminal)
# ---------------------------------------------------------------------------

def _viewer_login() -> str | None:
    """Best-effort GitHub login, used to tick pre-existing `topvoted` votes.

    Env overrides win; otherwise use the normal token chain (`_get_token` covers
    env, the `gh` CLI and the git credential helper) and ask GitHub who it is.
    Never raises or blocks: any failure yields None, and sessions that
    authenticate only through SSH catch up after their first `:w`.
    """
    user = os.getenv("CUHKVOTING_USER") or os.getenv("GITHUB_USER")
    if user and user.strip():
        return user.strip()
    try:
        token = cli._get_token()
        return cli._get_user_from_token(token) if token else None
    except Exception:  # noqa: BLE001 - highlighting must never block startup
        return None


def _mark_known_votes(session: Session, user: str | None) -> None:
    """Tick rows whose `voter_users` already contains `user` (topvoted lists).

    `voter_users` is filled by `_topvoted_rows_from_papers` from the records
    already fetched for the list, so this costs no extra network calls. Other
    list rows carry no voter identities, making this a no-op for them.
    """
    needle = (user or "").strip().lower()
    if not needle:
        return
    for paper in session.papers:
        if needle in (paper.entry.get("voter_users") or ()):
            session.voted.add(str(paper.entry.get("id", "")))


def _cast_votes(session: Session, cfg) -> bool:
    """Apply staged votes and removals. True when the batch went through."""
    staged = list(session.voting)
    removals = list(session.unvoting)
    entry_by_id = {str(p.entry.get("id", "")): p.entry for p in session.papers}

    def vote_meta(pid: str) -> dict:
        """Vote metadata from the in-memory list entry (abstract included, like plain vote)."""
        entry = entry_by_id.get(pid, {})
        return {
            "paper_id": pid,
            "title": " ".join(str(entry.get("title", "")).split()),
            "abstract": " ".join(str(entry.get("abstract", "")).split()),
            "url": entry.get("url") or f"{cli.ARXIV_ABS}{pid}",
        }

    try:
        token = cli._get_token()
        repo_cfg = cli._resolve_repo_config(
            SimpleNamespace(repo=None, branch=os.getenv("CUHKVOTING_BRANCH", "main")))
        use_api = cli._prefer_api_vote(token)

        metas, skipped = [], []
        # Cheap API skip-list only; SSH folds JC filtering into the vote checkout.
        if staged and use_api:
            selected = cli._selected_arxiv_ids(repo_cfg, token)
            for pid in staged:
                if cli._strip_arxiv_version(pid) in selected:
                    skipped.append(pid)
                else:
                    metas.append(vote_meta(pid))
        else:
            for pid in staged:
                metas.append(vote_meta(pid))
        for pid in skipped:
            session.warnings.append(f"Skipping {pid}: already selected for a past journal club.")
            session.session_votes.append(
                {"id": pid, "title": session.title_of(pid), "status": "skipped"})
        if use_api and not metas and not removals:
            session.voting = []  # selected papers can never be voted; unstage them
            session.feedback = "Nothing to vote for (all staged papers were already selected)"
            return True

        user = cli._resolve_user(token)
        if staged:
            cli._warn_if_display_name_changed(cfg.display_name)
        if use_api:
            result = cli._batch_vote_papers_api(
                repo_cfg, token, user, metas, cfg.display_name, removals=removals)
        elif cli._has_github_ssh_access():
            result = cli._batch_vote_papers_ssh(
                repo_cfg, user, metas, cfg.display_name, removals=removals)
        else:
            raise SystemExit(
                "Voting needs auth. Set CUHKVOTING_TOKEN/GITHUB_TOKEN or configure SSH key.\n\n"
                + cli._ssh_setup_instructions()
            )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        session.warnings.append(f"Vote update failed — HTTP {exc.code}: {body[:200]}")
        return False
    except urllib.error.URLError as exc:
        session.warnings.append(
            f"Vote update failed — network error: {exc.reason}. Changes kept staged; retry with :w")
        return False
    except (ConnectionError, TimeoutError, http.client.IncompleteRead) as exc:
        session.warnings.append(
            f"Vote update failed ({type(exc).__name__}: {exc}). "
            "Changes kept staged; retry with :w")
        return False
    except (RuntimeError, SystemExit) as exc:
        session.warnings.append(f"Vote update failed: {exc}")
        return False

    for pid in result.skipped_selected:
        session.warnings.append(f"Skipping {pid}: already selected for a past journal club.")
        session.session_votes.append(
            {"id": pid, "title": session.title_of(pid), "status": "skipped"})
    for pid in result.missing:
        session.warnings.append(f"No active vote to remove for {pid}.")

    # Session-local "already voted" marks: newly cast votes and duplicates (the
    # batch reports papers the user had voted for before this session). The
    # fallback also lights up the user's other `topvoted` rows: SSH-only
    # sessions only learn their login here, when the first write happens.
    session.voted.update(result.voted)
    _mark_known_votes(session, user)
    session.voted.difference_update(result.removed)

    duplicates = 0
    for pid in result.voted:
        status = "new" if pid in result.new else "duplicate"
        duplicates += status == "duplicate"
        session.session_votes.append({"id": pid, "title": session.title_of(pid), "status": status})
    for pid in result.removed:
        session.session_votes.append(
            {"id": pid, "title": session.title_of(pid), "status": "removed"})
    if result.outdated_msg:
        session.outdated_msg = result.outdated_msg
        session.warnings.append(result.outdated_msg.splitlines()[0])

    session.voting = []
    session.unvoting = []
    if not (result.voted or result.removed or result.missing):
        session.feedback = "Nothing to vote for (all staged papers were already selected)"
    else:
        parts = [f"{len(result.new)} vote(s) recorded"]
        if result.removed:
            parts.append(f"{len(result.removed)} vote(s) removed")
        if duplicates:
            parts.append(f"{duplicates} duplicate(s)")
        if result.missing:
            parts.append(f"{len(result.missing)} removal(s) skipped")
        session.feedback = ", ".join(parts)
    return True


def _print_exit_summary(session: Session) -> None:
    rows = session.session_votes
    if not rows:
        print("No votes cast this session.")
    else:
        print("Changes this session:")
        legend: dict[str, str] = {}
        for row in rows:
            glyph = ""
            if row["status"] == "duplicate":
                glyph = "  ✗"
                legend["✗"] = "duplicated an earlier vote (nothing changed)"
            elif row["status"] == "skipped":
                glyph = "  ⊘"
                legend["⊘"] = "skipped: already selected for a past journal club"
            elif row["status"] == "removed":
                glyph = f"  {REMOVAL_MARK}"
                legend[REMOVAL_MARK] = "vote removed"
            print(f"  {row['id']}  {row['title']}{glyph}")
        for glyph, meaning in legend.items():
            print(f"    {glyph} = {meaning}")
    if session.outdated_msg:
        print(typer.style(session.outdated_msg, fg=typer.colors.YELLOW))


def _open_current_url(session: Session) -> None:
    paper = session.current()
    if paper is None:
        return
    pid = str(paper.entry.get("id", ""))
    url = paper.entry.get("url") or f"{cli.ARXIV_ABS}{pid}"
    # The browser must not write to the terminal: fd-level silence, not just sys.stdout.
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.name == "posix":
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(url)
        session.feedback = f"Opened {url}"
    except OSError:
        session.feedback = f"Could not open a browser for {url}"


# ---------------------------------------------------------------------------
# UI assembly (needs prompt_toolkit)
# ---------------------------------------------------------------------------

if PROMPT_TOOLKIT_OK:

    class _ThemeCompleter(Completer):
        """Completes theme names, only for `:theme <prefix>`."""

        def __init__(self, tui: "Tui") -> None:
            self.tui = tui

        def get_completions(self, document, complete_event):
            if self.tui.cmd_prefix != ":":
                return
            text = document.text_before_cursor
            if not text.startswith("theme "):
                return
            prefix = text[len("theme "):]
            for name in THEMES:
                if name.startswith(prefix):
                    yield Completion(name, start_position=-len(prefix))

    class _WheelWindow(Window):
        """Window whose mouse-wheel events drive the list (scroll + edge drag).

        prompt_toolkit clamps the viewport to the cursor, so scrolling past the
        selection is only possible by moving the selection along; `on_wheel`
        implements that, and forwarding the sticky-header row keeps the wheel
        working over the pinned bar.
        """

        def __init__(self, on_wheel, *args, **kwargs) -> None:
            self._on_wheel = on_wheel
            super().__init__(*args, **kwargs)

        def _mouse_handler(self, mouse_event):
            if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                self._on_wheel(1)
                return None
            if mouse_event.event_type == MouseEventType.SCROLL_UP:
                self._on_wheel(-1)
                return None
            return super()._mouse_handler(mouse_event)


class Tui:
    """Owns the Session, the input buffer, the style, and the Application."""

    def __init__(self, session: Session, cfg, icfg: InteractiveConfig,
                 keymap: dict[str, list[str]], *, input=None, output=None) -> None:
        self.session = session
        self.cfg = cfg
        self.icfg = icfg
        self.keymap = keymap
        self.cmd_prefix = ""
        self.style = _theme_style(session.theme)
        self._frag_cache: dict = {}
        self._last_width: int | None = None
        self._line_owner: list[tuple[str, object]] = []   # content line -> ("day", d) | ("paper", index)
        self._paper_spans: dict[int, tuple[int, int]] = {}  # paper index -> (start, end) lines
        self._paper_days: dict[int, str] = {}               # paper index -> section day
        self._last_point = Point(x=0, y=0)
        self.buffer = Buffer(
            multiline=False,
            completer=_ThemeCompleter(self),
            complete_while_typing=False,
            accept_handler=self._accept,
            on_text_changed=self._on_text_changed,
        )
        self.app = Application(
            layout=_build_layout(self),
            key_bindings=_build_key_bindings(self),
            style=DynamicStyle(lambda: self.style),
            full_screen=True,
            mouse_support=True,
            input=input,
            output=output,
        )

    # -- input line --------------------------------------------------------

    def enter_cmd(self, prefix: str) -> None:
        self.session.mode = CMD
        self.session.feedback = ""
        self.cmd_prefix = prefix
        self.buffer.reset()
        self.app.layout.focus(self.input_window)

    def leave_cmd(self) -> None:
        self.session.mode = NOR
        self.cmd_prefix = ""
        self.buffer.reset()
        self.style = _theme_style(self.session.theme)  # drop any live preview
        self.app.layout.focus(self.body_window)

    def _accept(self, buffer: Buffer) -> bool:
        prefix, text = self.cmd_prefix, buffer.text
        self.leave_cmd()
        if prefix == ":":
            self._execute_command(text)
        elif prefix == "/":
            self.session.search(text, +1)
        elif prefix == "?":
            self.session.search(text, -1)
        return False  # never keep the text in the buffer

    def _on_text_changed(self, buffer: Buffer) -> None:
        """Live theme preview while typing `:theme <candidate>`."""
        if self.cmd_prefix != ":":
            return
        text = buffer.text.strip()
        if text.startswith("theme"):
            candidate = text[len("theme"):].strip()
            if candidate in THEMES:
                self.style = _theme_style(candidate)
                return
        self.style = _theme_style(self.session.theme)

    # -- `:` command execution ----------------------------------------------

    def _execute_command(self, text: str) -> None:
        s = self.session
        parsed = _parse_command(text)
        if parsed is None:
            s.feedback = f"Not a command: {text.strip()}"
            return
        name, payload = parsed
        if name == "jump":
            s.jump_index(payload)
        elif name == "quit":
            if s.voting or s.unvoting:
                s.feedback = "Staged changes not applied — :w to apply, :q! to discard"
            else:
                self.app.exit()
        elif name == "quit!":
            s.voting = []
            s.unvoting = []
            self.app.exit()
        elif name in ("write", "write-quit"):
            if not s.voting and not s.unvoting:
                s.feedback = "Nothing staged — nothing to write"
            else:
                s.exit_after_write = name == "write-quit"
                s.view = "confirm"
        elif name == "ls":
            s.enter_ls()
        elif name == "help":
            s.view = "help"
        elif name == "theme":
            self._set_theme(payload)

    def _set_theme(self, args: list[str]) -> None:
        s = self.session
        if len(args) != 1 or args[0] not in THEMES:
            s.feedback = "Themes: " + ", ".join(THEMES)
            return
        name = args[0]
        s.theme = name
        self.style = _theme_style(name)
        self._frag_cache.clear()
        error = _persist_theme(name)
        s.feedback = error if error else f"Theme '{name}' saved to config.toml"

    # -- rendering ---------------------------------------------------------

    def render_body(self) -> list:
        s = self.session
        if s.view == "help":
            self._last_point = Point(x=0, y=0)
            return _help_fragments(self.keymap)
        if s.view == "confirm":
            self._last_point = Point(x=0, y=0)
            return _confirm_fragments(s)

        width = max(20, self.app.output.get_size().columns)
        if width != self._last_width:  # every wrapped row depends on the width
            self._frag_cache.clear()
            self._last_width = width

        _has_ties, roles = _rank_roles(s.visible())
        frags: list = []
        line = 0
        cursor_line = 0
        day_seen = ""
        self._line_owner = []
        self._paper_spans = {}
        self._paper_days = {}
        for kind, payload in _display_items(s):
            if kind == "day":
                frags.append(("class:day", _day_rule(str(payload), width)))
                self._line_owner.append(("day", payload))
                line += 1
                day_seen = str(payload)
                continue
            paper = payload
            is_cur = paper is s.current()
            if is_cur:
                cursor_line = line
            role = roles.get(paper.index)
            pid = str(paper.entry.get("id", ""))
            key = (paper.index, s.abstract_open(paper), pid in s.voting, pid in s.voted,
                   pid in s.unvoting, is_cur, s.search_pat, s.theme, role)
            cached = self._frag_cache.get(key)
            if cached is None:
                cached = _entry_fragments(paper, s, self.cfg, width, role)
                self._frag_cache[key] = cached
            start = line
            frags.extend(cached)
            count = sum(text.count("\n") for _style, text in cached)
            self._paper_spans[paper.index] = (start, start + count)
            if day_seen:
                self._paper_days[paper.index] = day_seen
            for _ in range(count):
                self._line_owner.append(("paper", paper.index))
            line += count
        self._last_point = Point(x=0, y=cursor_line)
        return frags

    def render_sticky(self) -> list:
        """Pinned day header, shown once a section's inline row scrolled off."""
        s = self.session
        if s.view != "list" or not s.group_by_day:
            return []
        info = getattr(self.body_window, "render_info", None)
        if info is None:
            return []
        first = info.first_visible_line()
        if not 0 <= first < len(self._line_owner):
            return []
        kind, payload = self._line_owner[first]
        if kind != "paper":
            return []  # the inline day row itself is visible — no duplicate
        day = self._paper_days.get(payload)  # payload is the paper's full-list index
        if not day:
            return []
        width = max(1, self.app.output.get_size().columns)
        return [("class:day", _day_rule(day, width).rstrip("\n"))]

    def wheel(self, delta: int) -> None:
        """Mouse wheel: scroll the viewport; drag the selection at the edges.

        The selection's screen rows come from the last render's line spans plus
        the current scroll offset (not from `render_info`, which lags between
        consecutive wheel ticks), so ticks compose without an intermediate
        redraw.
        """
        s = self.session
        if s.mode != NOR or s.view != "list":
            return
        info = getattr(self.body_window, "render_info", None)
        if info is None or info.window_height <= 0:
            return
        vis = s.visible()
        if not vis:
            return
        scroll = self.body_window.vertical_scroll
        span = self._paper_spans.get(s.current().index) if s.current() else None
        if delta > 0:
            top_row = (span[0] - scroll) if span else info.cursor_position.y
            if top_row > 0 or s.cursor >= len(vis) - 1:
                self._scroll_body(delta, info)
                return
            s.move(1)  # selection reached the top edge: drag it along
            span = self._paper_spans.get(s.current().index) if s.current() else None
            # Follow mode opens/closes folds with the move, so the last render's
            # spans are stale; let prompt_toolkit clamp the scroll there.
            if span and not s.follow:  # pin the dragged block's first line
                self.body_window.vertical_scroll = span[0]
        else:
            bottom_row = (span[1] - 1 - scroll) if span else info.cursor_position.y
            if bottom_row < info.window_height - 1 or s.cursor <= 0:
                self._scroll_body(delta, info)
                return
            s.move(-1)
            span = self._paper_spans.get(s.current().index) if s.current() else None
            if span and not s.follow:  # pin the dragged block's last line
                self.body_window.vertical_scroll = max(0, span[1] - info.window_height)

    def _scroll_body(self, delta: int, info) -> None:
        top = self.body_window.vertical_scroll
        max_top = max(0, info.content_height - info.window_height)
        self.body_window.vertical_scroll = min(max(0, top + delta), max_top)

    def render_idle_line(self) -> list:
        s = self.session
        if s.pending_count:
            return [("", s.pending_count)]
        if s.feedback:
            return []
        if s.view == "confirm":
            return [("class:hint", "y confirm  n/esc cancel")]
        if s.view == "help":
            return [("class:hint", "esc back")]
        if self.icfg.key_hints:
            width = get_app().output.get_size().columns - 2
            return [("class:hint", _hint_strip(self.keymap, width))]
        return []


def _build_layout(tui: Tui) -> "Layout":
    s = tui.session
    in_cmd = Condition(lambda: s.mode == CMD)

    warning_bar = ConditionalContainer(
        Window(
            FormattedTextControl(lambda: _warning_fragments(s)),
            dont_extend_height=True,
            wrap_lines=True,
            style="class:warning",
        ),
        filter=Condition(lambda: bool(s.warnings)),
    )

    tui.body_window = _WheelWindow(
        tui.wheel,
        FormattedTextControl(
            tui.render_body,
            focusable=True,
            show_cursor=False,
            get_cursor_position=lambda: tui._last_point,
        ),
        wrap_lines=True,
    )

    # Pinned day header: a 1-row float over the top of the list. It is drawn
    # after the body (same frame), so it can read the body's fresh scroll state.
    sticky_header = Float(
        _WheelWindow(tui.wheel, FormattedTextControl(tui.render_sticky), height=1),
        top=0,
        left=0,
    )
    body_area = FloatContainer(content=tui.body_window, floats=[sticky_header])

    status_bar = VSplit(
        [
            Window(FormattedTextControl(lambda: f" {s.mode} "), dont_extend_width=True),
            Window(FormattedTextControl("")),  # spacer
            Window(FormattedTextControl(lambda: f"{s.status_right()} "),
                   dont_extend_width=True),
        ],
        height=1,
        style="class:status",
    )

    tui.input_window = Window(BufferControl(tui.buffer), height=1)
    cmd_line = ConditionalContainer(
        VSplit(
            [
                Window(FormattedTextControl(lambda: tui.cmd_prefix), dont_extend_width=True),
                tui.input_window,
                Window(
                    FormattedTextControl(
                        lambda: _command_hint(tui.cmd_prefix, tui.buffer.text)),
                    dont_extend_width=True,
                    style="class:hint",
                ),
            ],
            height=1,
        ),
        filter=in_cmd,
    )
    idle_line = ConditionalContainer(
        VSplit(
            [
                Window(FormattedTextControl(tui.render_idle_line), height=1),
                Window(FormattedTextControl(lambda: s.feedback),
                       dont_extend_width=True, style="class:feedback"),
            ],
            height=1,
        ),
        filter=~in_cmd,
    )

    root = HSplit([warning_bar, body_area, status_bar, cmd_line, idle_line],
                  style="class:body")
    layout = Layout(root)
    layout.focus(tui.body_window)
    return layout


def _build_key_bindings(tui: Tui) -> "KeyBindings":
    kb = KeyBindings()
    s = tui.session

    in_list = Condition(lambda: s.mode == NOR and s.view == "list")
    in_confirm = Condition(lambda: s.mode == NOR and s.view == "confirm")
    in_help = Condition(lambda: s.mode == NOR and s.view == "help")
    in_cmd = Condition(lambda: s.mode == CMD)

    def bind(action: str, handler, filter=in_list) -> None:
        def wrapped(event) -> None:
            s.feedback = ""
            handler(event)
        for spec in tui.keymap[action]:
            kb.add(*spec.split(), filter=filter)(wrapped)

    def page_size(event) -> int:
        return max(1, event.app.output.get_size().rows - 4)

    bind("move_down", lambda e: s.move(s.take_count()))
    bind("move_up", lambda e: s.move(-s.take_count()))
    bind("page_down", lambda e: s.move(page_size(e)))
    bind("page_up", lambda e: s.move(-page_size(e)))
    bind("add", lambda e: s.add_current())
    bind("remove", lambda e: s.remove_current())
    bind("open_url", lambda e: _open_current_url(s))
    bind("toggle_abstract", lambda e: s.toggle_abstract())
    bind("open_abstract", lambda e: s.open_abstract())
    bind("close_abstract", lambda e: s.close_abstract())
    bind("open_all", lambda e: s.open_all())
    bind("close_all", lambda e: s.close_all())
    bind("toggle_follow", lambda e: s.toggle_follow())
    bind("next_match", lambda e: s.next_match())
    bind("prev_match", lambda e: s.next_match(reverse=True))
    bind("command", lambda e: tui.enter_cmd(":"))
    bind("search_fwd", lambda e: tui.enter_cmd("/"))
    bind("search_bwd", lambda e: tui.enter_cmd("?"))

    def goto(default_jump):
        def handler(event) -> None:  # vim: <#>gg / <#>G jump to paper <#>
            s.feedback = ""
            if s.pending_count:
                s.jump_index(int(s.pending_count))
                s.pending_count = ""
            else:
                default_jump()
        return handler
    for spec in tui.keymap["top"]:
        kb.add(*spec.split(), filter=in_list)(goto(s.jump_first))
    for spec in tui.keymap["bottom"]:
        kb.add(*spec.split(), filter=in_list)(goto(s.jump_last))

    def add_digit(digit: str):
        def handler(event) -> None:
            s.pending_count += digit
        return handler
    for digit in "123456789":
        kb.add(digit, filter=in_list)(add_digit(digit))
    kb.add("0", filter=in_list & Condition(lambda: bool(s.pending_count)))(add_digit("0"))

    @kb.add("escape", filter=in_list)
    def _escape_list(event) -> None:
        s.feedback = ""
        if s.pending_count:
            s.pending_count = ""
        elif s.ls_only:
            s.exit_ls()

    @kb.add("escape", filter=in_help)
    def _escape_help(event) -> None:
        s.view = "list"

    @kb.add("escape", filter=in_cmd, eager=True)
    def _escape_cmd(event) -> None:
        tui.leave_cmd()

    @kb.add("tab", filter=in_cmd)
    def _complete(event) -> None:
        tui.buffer.complete_next()

    @kb.add("y", filter=in_confirm)
    async def _confirm_yes(event) -> None:
        s.feedback = ""
        s.view = "list"
        outcome: dict = {}

        def blocking() -> None:
            outcome["ok"] = _cast_votes(s, tui.cfg)

        await run_in_terminal(blocking)
        tui._frag_cache.clear()  # staged marks changed
        if outcome.get("ok") and s.exit_after_write:
            event.app.exit()
        s.exit_after_write = False

    def _confirm_cancel(event) -> None:
        s.view = "list"
        s.exit_after_write = False
        s.feedback = "Vote aborted"
    kb.add("n", filter=in_confirm)(_confirm_cancel)
    kb.add("escape", filter=in_confirm)(_confirm_cancel)

    @kb.add("c-c", filter=~in_cmd)
    def _ctrl_c(event) -> None:
        if s.voting or s.unvoting:
            s.feedback = "Staged changes not applied — :w to apply, :q! to discard"
        else:
            event.app.exit()

    @kb.add("c-c", filter=in_cmd)
    def _ctrl_c_cmd(event) -> None:
        tui.leave_cmd()

    return kb


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(tokens: list[str]) -> int:
    if not PROMPT_TOOLKIT_OK:  # defensive: the CLI command gates on this too
        print(INSTALL_HINT, file=sys.stderr)
        return 1

    cfg = cli._load_config()
    try:
        data, captured, loader = _load_list(tokens, cfg)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='ignore')[:300]}",
              file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Network error: {exc.reason}", file=sys.stderr)
        return 1
    except (ConnectionError, TimeoutError, http.client.IncompleteRead) as exc:
        print(f"Fetch failed ({type(exc).__name__}: {exc}). "
              "This is usually temporary — retry in a moment.", file=sys.stderr)
        return 1

    if data.empty_msg:
        for line in captured:
            print(line)
        print(data.empty_msg)
        return 0

    cli._save_last_list(data.entries)  # keep `cuhkvoting vote <#>` working afterwards
    icfg = _load_interactive_config()
    keymap, key_problems = _effective_keymap(icfg.keys)
    theme = icfg.theme if icfg.theme in THEMES else "default"

    session = Session(data.entries, theme=theme)
    session.follow = icfg.follow
    # Day rows belong to date-sorted listings only; search results are ranked
    # by relevance and topvoted rows carry no publication date.
    session.group_by_day = loader in (
        cli._today_list, cli._lastweek_list, cli._lastdays_list, cli._show_date_list,
    )
    if loader is cli._topvoted_list:
        # The list rows already carry voter usernames; no extra fetch needed.
        _mark_known_votes(session, _viewer_login())
    session.warnings.extend(data.notes + captured + key_problems)

    tui = Tui(session, cfg, icfg, keymap)
    tui.app.run()
    _print_exit_summary(session)
    return 0


def main() -> None:  # pragma: no cover - convenience for `python -m cuhkvoting.interactive`
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
