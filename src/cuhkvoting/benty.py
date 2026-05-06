"""Benty-Fields → cuhkvoting sync addon.

Install with:  pip install "cuhkvoting[benty]"
Run with:      cuhkvoting-benty [--dry-run] [--no-cache-cookies]
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

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

_PAPER_ID_PAT = re.compile(
    r"paper_id=([0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-z\-]+/[0-9]{7})",
    re.IGNORECASE,
)

app = typer.Typer(help="Sync Benty-Fields journal-club papers to cuhkvoting.")


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


# ---------------------------------------------------------------------------
# Paper extraction
# ---------------------------------------------------------------------------

def _extract_papers(html: str) -> list[dict]:
    """Return list of {arxiv_id, title} dicts found in manage_jc HTML.

    Each paper row contains a /daily_arXiv_results?paper_id=...&dbname=arxiv
    link whose text is the title. Non-arXiv entries (different dbname) are
    returned with arxiv_id=None so callers can warn about them.
    """
    soup = BeautifulSoup(html, "html.parser")
    papers: list[dict] = []
    seen: set[str] = set()

    for row in soup.find_all("tr", class_="table_entry"):
        # Only process rows where the current user has voted (Unvote = btn-danger)
        if not row.find("button", class_="btn-danger"):
            continue
        entry: dict = {"arxiv_id": None, "title": None}
        for a in row.find_all("a", href=True):
            href = a["href"]
            if "daily_arXiv_results" not in href:
                continue
            if "dbname=arxiv" not in href:
                # Non-arXiv paper — capture title but no ID
                entry["title"] = a.get_text(strip=True) or None
                continue
            m = _PAPER_ID_PAT.search(href)
            if not m:
                continue
            arxiv_id = re.sub(r"v\d+$", "", m.group(1))
            if arxiv_id in seen:
                entry = None  # duplicate row, skip
                break
            seen.add(arxiv_id)
            entry["arxiv_id"] = arxiv_id
            entry["title"] = a.get_text(strip=True) or None
            break
        if entry is not None:
            papers.append(entry)

    return papers


# ---------------------------------------------------------------------------
# Synced-ID cache
# ---------------------------------------------------------------------------

def _load_synced() -> set[str]:
    if SYNCED_PATH.exists():
        return set(json.loads(SYNCED_PATH.read_text()))
    return set()


def _save_synced(ids: set[str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SYNCED_PATH.write_text(json.dumps(sorted(ids)))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@app.command()
def sync(
    dry_run: bool = typer.Option(False, "--dry-run", help="List papers without voting."),
    no_cache_cookies: bool = typer.Option(
        False, "--no-cache-cookies", help="Do not persist session cookies."
    ),
    dump_html: bool = typer.Option(
        False, "--dump-html", help="Save manage_jc HTML to /tmp/manage_jc.html and exit.",
        hidden=True,
    ),
) -> None:
    """Fetch papers from Benty-Fields manage_jc and vote for new ones."""
    if not _BS4_OK:
        typer.echo(
            'beautifulsoup4 is required. Install with: pip install "cuhkvoting[benty]"',
            err=True,
        )
        raise typer.Exit(1)

    cache_cookies = _cache_cookies_enabled() and not no_cache_cookies

    jar: http.cookiejar.CookieJar = http.cookiejar.CookieJar()
    cookies_loaded = cache_cookies and _load_cookies(jar)
    opener = _make_opener(jar)

    # Try cached session first; fall back to fresh login
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

    papers = _extract_papers(html)

    if not papers:
        typer.echo("No papers with arXiv links found on manage_jc.")
        return

    for p in papers:
        if p["arxiv_id"] is None:
            label = p["title"] or "(unknown title)"
            typer.echo(f"Warning: skipping non-arXiv paper: {label}", err=True)

    arxiv_papers = [p for p in papers if p["arxiv_id"] is not None]
    synced = _load_synced()
    new_papers = [p for p in arxiv_papers if p["arxiv_id"] not in synced]

    if not new_papers:
        typer.echo(f"All {len(arxiv_papers)} paper(s) already synced.")
        return

    typer.echo(f"Found {len(new_papers)} new paper(s):")
    for p in new_papers:
        title = p["title"] or "(no title)"
        typer.echo(f"  {p['arxiv_id']}  {title}")

    if dry_run:
        typer.echo("Dry run — no votes cast.")
        return

    result = subprocess.run(
        [sys.executable, "-m", "cuhkvoting", "vote"] + [p["arxiv_id"] for p in new_papers]
    )
    if result.returncode == 0:
        synced.update(p["arxiv_id"] for p in new_papers)
        _save_synced(synced)
        typer.echo(f"Synced {len(new_papers)} paper(s).")
    else:
        typer.echo("Vote command failed; synced cache not updated.", err=True)
        raise typer.Exit(result.returncode)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
