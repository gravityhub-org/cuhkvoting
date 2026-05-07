"""Benty-Fields ↔ cuhkvoting two-way sync addon.

Install with:  pip install "cuhkvoting[benty]"
Run with:      cuhkvoting-benty [--dry-run] [--no-cache-cookies]

Vote endpoint:   POST /process_paper_vote  JSON {"group_id", "paper_id", "dbname"}
Unvote endpoint: POST /remove_vote         JSON {"vote_id"}  (internal Benty DB id)
"""
from __future__ import annotations

import datetime as dt
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import typer

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

try:
    from bs4 import BeautifulSoup  # type: ignore
    _BS4_OK = True
except ImportError:
    _BS4_OK = False

BENTY_BASE = "https://www.benty-fields.com"
CACHE_DIR = Path.home() / ".cache" / "cuhkvoting"
SYNCED_PATH = CACHE_DIR / "benty_synced.json"
COOKIES_PATH = CACHE_DIR / "benty_cookies.json"
CONFIG_PATH = Path.home() / ".config" / "cuhkvoting" / "config.toml"
EXPIRY_DAYS = 183

_PAPER_ID_PAT = re.compile(
    r"paper_id=([0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-z\-]+/[0-9]{7})",
    re.IGNORECASE,
)

app = typer.Typer(help="Two-way sync between Benty-Fields and cuhkvoting.")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _cache_cookies_enabled() -> bool:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
        return cfg.get("benty", {}).get("cache_cookies", True)
    return True


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _get_credentials() -> tuple[str, str]:
    """Return (email, password) from git credential helper, or interactive prompt."""
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=benty-fields.com\n\n",
            capture_output=True, text=True, timeout=5,
        )
        creds: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
        if creds.get("username") and creds.get("password"):
            return creds["username"], creds["password"]
    except Exception:
        pass
    email = typer.prompt("Benty-Fields email")
    password = typer.prompt("Benty-Fields password", hide_input=True)
    return email, password


# ---------------------------------------------------------------------------
# Cookie persistence
# ---------------------------------------------------------------------------

def _make_opener(jar: http.cookiejar.CookieJar) -> urllib.request.OpenerDirector:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", "cuhkvoting/0.1 (benty-sync)")]
    return opener


def _save_cookies(jar: http.cookiejar.CookieJar) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    COOKIES_PATH.write_text(json.dumps([
        {
            "name": c.name, "value": c.value,
            "domain": c.domain, "path": c.path,
            "secure": c.secure, "expires": c.expires,
        }
        for c in jar
    ]))


def _load_cookies(jar: http.cookiejar.CookieJar) -> bool:
    if not COOKIES_PATH.exists():
        return False
    try:
        for c in json.loads(COOKIES_PATH.read_text()):
            jar.set_cookie(http.cookiejar.Cookie(
                version=0,
                name=c["name"], value=c["value"],
                port=None, port_specified=False,
                domain=c["domain"], domain_specified=True,
                domain_initial_dot=c["domain"].startswith("."),
                path=c["path"], path_specified=True,
                secure=c["secure"], expires=c.get("expires"),
                discard=False, comment=None, comment_url=None, rest={},
            ))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_csrf_token(opener: urllib.request.OpenerDirector) -> str:
    with opener.open(f"{BENTY_BASE}/login") as r:
        html = r.read().decode("utf-8")
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html) or \
        re.search(r'value="([^"]+)"[^>]*name="csrf_token"', html)
    if not m:
        raise RuntimeError("CSRF token not found on Benty-Fields login page")
    return m.group(1)


def _login(opener: urllib.request.OpenerDirector, email: str, password: str) -> None:
    csrf = _get_csrf_token(opener)
    data = urllib.parse.urlencode(
        {"csrf_token": csrf, "email": email, "password": password, "next": ""}
    ).encode()
    req = urllib.request.Request(
        f"{BENTY_BASE}/login", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener.open(req) as r:
        final_url = r.geturl()
    if "/login" in final_url:
        raise RuntimeError("Login failed — check your Benty-Fields credentials")


def _fetch_page(opener: urllib.request.OpenerDirector, url: str) -> tuple[str, str]:
    """Return (html, final_url)."""
    with opener.open(url) as r:
        return r.read().decode("utf-8"), r.geturl()


def _post_json(opener: urllib.request.OpenerDirector, url: str, payload: dict) -> dict:
    """POST JSON payload, return parsed JSON response."""
    data = json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with opener.open(req) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# Benty-Fields vote / unvote
# ---------------------------------------------------------------------------

def _benty_vote(opener: urllib.request.OpenerDirector, arxiv_id: str, group_id: str) -> None:
    """Cast a vote on Benty-Fields. Adds paper to the manage_jc agenda if absent."""
    resp = _post_json(opener, f"{BENTY_BASE}/process_paper_vote", {
        "group_id": group_id,
        "paper_id": arxiv_id,
        "dbname": "arxiv",
    })
    if resp.get("error"):
        raise RuntimeError(resp["error"])


def _benty_unvote(opener: urllib.request.OpenerDirector, vote_id: str) -> None:
    """Remove a vote on Benty-Fields using the internal vote record ID."""
    resp = _post_json(opener, f"{BENTY_BASE}/remove_vote", {"vote_id": vote_id})
    if resp.get("error"):
        raise RuntimeError(resp["error"])


def _fetch_benty_vote_id(
    opener: urllib.request.OpenerDirector, arxiv_id: str
) -> str | None:
    """Re-fetch manage_jc to retrieve the internal vote_id for a given arXiv ID."""
    html, _ = _fetch_page(opener, f"{BENTY_BASE}/manage_jc")
    papers = _extract_papers(html)
    for p in papers:
        if p["arxiv_id"] == arxiv_id and p["user_voted"]:
            return p["vote_id"]
    return None


# ---------------------------------------------------------------------------
# Paper extraction
# ---------------------------------------------------------------------------

def _extract_papers(html: str) -> list[dict]:
    """Return all paper rows as {arxiv_id, title, user_voted, vote_id} dicts.

    user_voted=True when the row has an Unvote (btn-danger) button.
    vote_id is the internal Benty DB id from the "remove" dropdown, needed for
    _benty_unvote(). Non-arXiv entries are returned with arxiv_id=None.
    """
    soup = BeautifulSoup(html, "html.parser")
    papers: list[dict] = []
    seen: set[str] = set()

    for row in soup.find_all("tr", class_="table_entry"):
        user_voted = bool(row.find("button", class_="btn-danger"))

        # Internal vote_id lives in the value attr of the remove dropdown link
        vote_id: str | None = None
        for a in row.find_all("a"):
            if "remove_vote" in a.get("onclick", ""):
                vote_id = a.get("value")
                break

        entry: dict = {
            "arxiv_id": None, "title": None,
            "user_voted": user_voted, "vote_id": vote_id,
        }
        for a in row.find_all("a", href=True):
            href = a["href"]
            if "daily_arXiv_results" not in href:
                continue
            if "dbname=arxiv" not in href:
                entry["title"] = a.get_text(strip=True) or None
                continue
            m = _PAPER_ID_PAT.search(href)
            if not m:
                continue
            arxiv_id = re.sub(r"v\d+$", "", m.group(1))
            if arxiv_id in seen:
                entry = None  # type: ignore[assignment]
                break
            seen.add(arxiv_id)
            entry["arxiv_id"] = arxiv_id
            entry["title"] = a.get_text(strip=True) or None
            break
        if entry is not None:
            papers.append(entry)

    return papers


def _extract_group_id(html: str) -> str | None:
    m = re.search(r"/manage_jc\?groupid=(\d+)", html)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# cuhkvoting vote reader
# ---------------------------------------------------------------------------

def _get_cuhk_voted_ids(user: str) -> dict[str, dict]:
    """Return {arxiv_id: {"voted_at": str|None, "title": str|None}} for user's active votes."""
    from cuhkvoting.cli import (  # noqa: PLC0415
        DEFAULT_REPO, _get_token, _list_papers_via_api, _resolve_repo_config,
    )

    token = _get_token()
    branch = os.getenv("CUHKVOTING_BRANCH", "main")
    cfg = _resolve_repo_config(SimpleNamespace(repo=DEFAULT_REPO, branch=branch))
    papers = _list_papers_via_api(cfg, token)

    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=EXPIRY_DAYS)

    result: dict[str, dict] = {}
    for paper in papers:
        raw_id = paper.get("id", "")
        arxiv_id = re.sub(r"v\d+$", "", raw_id) if raw_id else None
        if not arxiv_id:
            continue
        for vote in paper.get("votes", []):
            if vote.get("user") != user:
                continue
            voted_at_str = vote.get("voted_at")
            if voted_at_str:
                try:
                    voted_at = dt.datetime.fromisoformat(voted_at_str.replace("Z", "+00:00"))
                    if voted_at < cutoff:
                        continue  # expired
                except ValueError:
                    pass
            result[arxiv_id] = {"voted_at": voted_at_str, "title": paper.get("title")}
            break

    return result


# ---------------------------------------------------------------------------
# Synced-ID cache
# Format: {arxiv_id: {"voted_at": str | None, "benty_vote_id": str | None}}
# ---------------------------------------------------------------------------

def _load_synced() -> dict[str, dict]:
    if not SYNCED_PATH.exists():
        return {}
    data = json.loads(SYNCED_PATH.read_text())
    if isinstance(data, list):
        # Migrate from old list-of-IDs format
        return {arxiv_id: {"voted_at": None, "benty_vote_id": None} for arxiv_id in data}
    # Backfill missing benty_vote_id key for entries written before this field existed
    for meta in data.values():
        meta.setdefault("benty_vote_id", None)
    return data


def _save_synced(synced: dict[str, dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SYNCED_PATH.write_text(json.dumps(synced, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@app.command()
def sync(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without making changes."),
    no_cache_cookies: bool = typer.Option(
        False, "--no-cache-cookies", help="Do not persist session cookies."
    ),
    dump_html: bool = typer.Option(
        False, "--dump-html", help="Save manage_jc HTML to /tmp/manage_jc.html and exit.",
        hidden=True,
    ),
) -> None:
    """Two-way sync between Benty-Fields manage_jc and cuhkvoting."""
    if not _BS4_OK:
        typer.echo(
            'beautifulsoup4 is required. Install with: pip install "cuhkvoting[benty]"',
            err=True,
        )
        raise typer.Exit(1)

    # Resolve cuhkvoting user (GitHub username)
    from cuhkvoting.cli import _get_token, _resolve_user  # noqa: PLC0415
    gh_token = _get_token()
    try:
        cuhk_user = _resolve_user(gh_token)
    except SystemExit as exc:
        typer.echo(f"Error resolving cuhkvoting user: {exc}", err=True)
        raise typer.Exit(1)

    # Login to Benty-Fields
    cache_cookies = _cache_cookies_enabled() and not no_cache_cookies
    jar: http.cookiejar.CookieJar = http.cookiejar.CookieJar()
    cookies_loaded = cache_cookies and _load_cookies(jar)
    opener = _make_opener(jar)

    if cookies_loaded:
        html, final_url = _fetch_page(opener, f"{BENTY_BASE}/manage_jc")
        needs_login = "/login" in final_url
    else:
        needs_login = True
        html = ""

    if needs_login:
        email, password = _get_credentials()
        try:
            _login(opener, email, password)
        except RuntimeError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)
        html, final_url = _fetch_page(opener, f"{BENTY_BASE}/manage_jc")
        if "/login" in final_url:
            typer.echo("Error: could not access manage_jc after login.", err=True)
            raise typer.Exit(1)
        if cache_cookies:
            _save_cookies(jar)

    if dump_html:
        out = Path("/tmp/manage_jc.html")
        out.write_text(html)
        typer.echo(f"HTML saved to {out}")
        return

    group_id = _extract_group_id(html)
    if not group_id:
        typer.echo(
            typer.style("Warning: could not determine Benty-Fields group ID.", fg=typer.colors.CYAN),
            err=True,
        )

    # Collect state from both systems
    benty_papers = _extract_papers(html)
    benty_voted_ids = {p["arxiv_id"] for p in benty_papers if p["user_voted"] and p["arxiv_id"]}
    benty_by_id = {p["arxiv_id"]: p for p in benty_papers if p["arxiv_id"]}

    for p in benty_papers:
        if p["arxiv_id"] is None:
            label = p["title"] or "(unknown title)"
            typer.echo(
                typer.style(f"Warning: skipping non-arXiv paper: {label}", fg=typer.colors.CYAN),
                err=True,
            )

    cuhk_voted = _get_cuhk_voted_ids(cuhk_user)  # {arxiv_id: {"voted_at", "title"}}
    cuhk_voted_ids = set(cuhk_voted)

    synced = _load_synced()  # {arxiv_id: {"voted_at", "benty_vote_id"}}
    synced_ids = set(synced)

    # Opportunistically update benty_vote_id for synced papers where it's missing
    for arxiv_id, meta in synced.items():
        if meta.get("benty_vote_id") is None and arxiv_id in benty_by_id:
            meta["benty_vote_id"] = benty_by_id[arxiv_id].get("vote_id")

    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=EXPIRY_DAYS)

    # Compute what needs to change
    to_add_cuhk = [benty_by_id[i] for i in (benty_voted_ids - synced_ids)]
    to_add_benty = sorted(cuhk_voted_ids - synced_ids)  # any arXiv paper is voteable on Benty

    removed_from_benty = synced_ids - benty_voted_ids
    removed_from_cuhk = synced_ids - cuhk_voted_ids
    removed_from_both = removed_from_benty & removed_from_cuhk

    to_remove_cuhk = sorted(removed_from_benty - removed_from_both)

    to_remove_benty: list[str] = []
    expired_ids: list[str] = []
    for arxiv_id in sorted(removed_from_cuhk - removed_from_both):
        voted_at_str = synced[arxiv_id].get("voted_at")
        if not voted_at_str:
            expired_ids.append(arxiv_id)
            continue
        try:
            voted_at = dt.datetime.fromisoformat(voted_at_str.replace("Z", "+00:00"))
            if voted_at < cutoff:
                expired_ids.append(arxiv_id)  # natural expiry → leave Benty alone
            else:
                to_remove_benty.append(arxiv_id)  # explicit removal → propagate
        except ValueError:
            expired_ids.append(arxiv_id)

    nothing_to_do = (
        not to_add_cuhk and not to_remove_cuhk
        and not to_add_benty and not to_remove_benty
        and not removed_from_both and not expired_ids
    )
    if nothing_to_do:
        typer.echo(f"All papers already in sync ({len(synced_ids)} tracked).")
        return

    # Print summary
    if to_add_cuhk:
        typer.echo(f"Adding to cuhkvoting ({len(to_add_cuhk)}):")
        for p in to_add_cuhk:
            typer.echo(typer.style(
                f"  + {p['arxiv_id']}  {p['title'] or ''}", fg=typer.colors.GREEN
            ))

    if to_remove_cuhk:
        typer.echo(f"Removing from cuhkvoting ({len(to_remove_cuhk)}):")
        for arxiv_id in to_remove_cuhk:
            typer.echo(typer.style(f"  - {arxiv_id}", fg=typer.colors.RED))

    if to_add_benty:
        typer.echo(f"Adding to Benty-Fields ({len(to_add_benty)}):")
        for arxiv_id in to_add_benty:
            title = cuhk_voted[arxiv_id].get("title") or ""
            typer.echo(typer.style(f"  + {arxiv_id}  {title}", fg=typer.colors.GREEN))

    if to_remove_benty:
        typer.echo(f"Removing from Benty-Fields ({len(to_remove_benty)}):")
        for arxiv_id in to_remove_benty:
            typer.echo(typer.style(f"  - {arxiv_id}", fg=typer.colors.RED))

    if dry_run:
        typer.echo("Dry run — no changes made.")
        return

    # Execute changes
    ok = True
    ts_now = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if to_add_cuhk:
        result = subprocess.run(
            [sys.executable, "-m", "cuhkvoting", "vote"] + [p["arxiv_id"] for p in to_add_cuhk]
        )
        if result.returncode == 0:
            for p in to_add_cuhk:
                synced[p["arxiv_id"]] = {"voted_at": ts_now, "benty_vote_id": p.get("vote_id")}
        else:
            typer.echo("cuhkvoting vote failed.", err=True)
            ok = False

    for arxiv_id in to_remove_cuhk:
        result = subprocess.run(
            [sys.executable, "-m", "cuhkvoting", "vote", "remove", arxiv_id]
        )
        if result.returncode == 0:
            synced.pop(arxiv_id, None)
        else:
            typer.echo(f"cuhkvoting vote remove failed for {arxiv_id}.", err=True)
            ok = False

    if not group_id and to_add_benty:
        typer.echo("Error: cannot add to Benty-Fields — group ID unknown.", err=True)
        ok = False
    else:
        for arxiv_id in to_add_benty:
            try:
                _benty_vote(opener, arxiv_id, group_id)  # type: ignore[arg-type]
                synced[arxiv_id] = {"voted_at": cuhk_voted[arxiv_id].get("voted_at"), "benty_vote_id": None}
            except RuntimeError as exc:
                typer.echo(f"Benty-Fields vote failed for {arxiv_id}: {exc}", err=True)
                ok = False

    for arxiv_id in to_remove_benty:
        vote_id = synced.get(arxiv_id, {}).get("benty_vote_id")
        if not vote_id:
            # vote_id missing — re-fetch manage_jc to find it
            typer.echo(
                typer.style(f"  Fetching vote_id for {arxiv_id}…", fg=typer.colors.CYAN)
            )
            vote_id = _fetch_benty_vote_id(opener, arxiv_id)
        if not vote_id:
            typer.echo(
                typer.style(
                    f"  Warning: cannot unvote {arxiv_id} on Benty-Fields — vote_id unknown.",
                    fg=typer.colors.CYAN,
                ),
                err=True,
            )
            ok = False
            continue
        try:
            _benty_unvote(opener, vote_id)
            synced.pop(arxiv_id, None)
        except RuntimeError as exc:
            typer.echo(f"Benty-Fields unvote failed for {arxiv_id}: {exc}", err=True)
            ok = False

    # Clean up: removed from both systems and naturally expired entries
    for arxiv_id in list(removed_from_both) + expired_ids:
        synced.pop(arxiv_id, None)

    _save_synced(synced)

    if ok:
        adds = len(to_add_cuhk) + len(to_add_benty)
        removes = len(to_remove_cuhk) + len(to_remove_benty)
        typer.echo(f"Sync complete: +{adds} / -{removes}.")
    else:
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
