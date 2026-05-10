from __future__ import annotations

import base64
import concurrent.futures
import http.client
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import typer


USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0"
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_ABS = "https://arxiv.org/abs/"
ARXIV_FOUNDING_DATE = dt.date(1991, 8, 14)
INSPIRE_API = "https://inspirehep.net/api/literature"
DEFAULT_REPO = "gravityhub-org/cuhkvoting-records"
VOTE_EXPIRY_DAYS = 183
JC_RECORD_PATH = "papers/journal_club_records.json"
def _user_config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home()) / "cuhkvoting"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "cuhkvoting"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "cuhkvoting"


def _user_cache_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "cuhkvoting" / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "cuhkvoting"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "cuhkvoting"


CACHE_DIR = _user_cache_dir()
LAST_LIST_PATH = CACHE_DIR / "last_list.json"
DISPLAY_NAME_CACHE = CACHE_DIR / "display_name.txt"
DISPLAY_NAMES_PATH = "display_names.json"
CONFIG_PATH = _user_config_dir() / "config.toml"
DEFAULT_CATEGORIES = ["gr-qc", "astro-ph.*"]
DEFAULT_TODAY_MAX_AGE = 60
DEFAULT_LASTWEEK_MAX_AGE = 360
DEFAULT_ABSTRACT_LINES = 0
DEFAULT_ABSTRACT_WRAP = 80
DEFAULT_HIGHLIGHT_AUTHORS: list[str] = []
DEFAULT_HIGHLIGHT_KEYWORDS: list[str] = []
DEFAULT_HIGHLIGHT_KEYWORD_COUNT = -1
DEFAULT_HIGHLIGHT_GLYPH = "★"


@dataclass
class RepoConfig:
    owner: str
    repo: str
    branch: str

    @property
    def ssh_clone_url(self) -> str:
        return f"git@github.com:{self.owner}/{self.repo}.git"


@dataclass
class Config:
    categories: list[str]
    today_max_age: int
    lastweek_max_age: int
    abstract_lines: int
    abstract_wrap: int
    confirm_by_number: bool
    display_name: str
    highlight_authors: list[str]
    highlight_keywords: list[str]
    highlight_keyword_count: int
    highlight_glyph: str


def _load_config() -> Config:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "rb") as f:
                raw = tomllib.load(f)
            cats = raw.get("categories", DEFAULT_CATEGORIES)
            cache_cfg = raw.get("cache", {})
            display_cfg = raw.get("display", {})
            vote_cfg = raw.get("vote", {})
            hl_cfg = raw.get("highlights", {})
            return Config(
                categories=cats if isinstance(cats, list) and cats else DEFAULT_CATEGORIES,
                today_max_age=int(cache_cfg.get("today_max_age", DEFAULT_TODAY_MAX_AGE)),
                lastweek_max_age=int(cache_cfg.get("lastweek_max_age", DEFAULT_LASTWEEK_MAX_AGE)),
                abstract_lines=int(display_cfg.get("abstract_lines", DEFAULT_ABSTRACT_LINES)),
                abstract_wrap=int(display_cfg.get("abstract_wrap", DEFAULT_ABSTRACT_WRAP)),
                confirm_by_number=bool(vote_cfg.get("confirm_by_number", True)),
                display_name=str(vote_cfg.get("display_name", "")),
                highlight_authors=list(hl_cfg.get("authors", DEFAULT_HIGHLIGHT_AUTHORS)),
                highlight_keywords=list(hl_cfg.get("keywords", DEFAULT_HIGHLIGHT_KEYWORDS)),
                highlight_keyword_count=int(hl_cfg.get("keyword_count", DEFAULT_HIGHLIGHT_KEYWORD_COUNT)),
                highlight_glyph=str(hl_cfg.get("glyph", DEFAULT_HIGHLIGHT_GLYPH)),
            )
        except Exception as exc:
            typer.echo(
                typer.style(f"Warning: could not parse {CONFIG_PATH}: {exc}", fg=typer.colors.YELLOW),
                err=True,
            )
    return Config(
        categories=DEFAULT_CATEGORIES,
        today_max_age=DEFAULT_TODAY_MAX_AGE,
        lastweek_max_age=DEFAULT_LASTWEEK_MAX_AGE,
        abstract_lines=DEFAULT_ABSTRACT_LINES,
        abstract_wrap=DEFAULT_ABSTRACT_WRAP,
        confirm_by_number=True,
        display_name="",
        highlight_authors=DEFAULT_HIGHLIGHT_AUTHORS,
        highlight_keywords=DEFAULT_HIGHLIGHT_KEYWORDS,
        highlight_keyword_count=DEFAULT_HIGHLIGHT_KEYWORD_COUNT,
        highlight_glyph=DEFAULT_HIGHLIGHT_GLYPH,
    )


def _http_text(url: str, headers: dict[str, str] | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _http_json(url: str, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_json_request(
    url: str,
    method: str,
    payload: dict,
    headers: dict[str, str] | None = None,
) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers or {},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_repo_url(url: str) -> tuple[str, str] | None:
    url = url.strip()
    ssh = re.match(r"git@github\.com:([^/]+)/(.+?)(?:\.git)?$", url)
    if ssh:
        return ssh.group(1), ssh.group(2)
    https = re.match(r"https://github\.com/([^/]+)/(.+?)(?:\.git)?$", url)
    if https:
        return https.group(1), https.group(2)
    return None



def _run_git(args: list[str], cwd: str | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or f"git {' '.join(args)} failed"
        if _looks_like_default_repo_write_access_error(args, err):
            err = f"{err}\n\n{_org_join_instructions()}"
        if _looks_like_github_auth_error(err):
            err = f"{err}\n\n{_ssh_setup_instructions()}"
        raise SystemExit(err)
    return proc.stdout


def _looks_like_github_auth_error(text: str) -> bool:
    low = text.lower()
    return (
        "github.com" in low
        and (
            "permission denied (publickey)" in low
            or "authentication failed" in low
            or "could not read from remote repository" in low
            or "could not read username for 'https://github.com'" in low
        )
    )


def _looks_like_default_repo_write_access_error(args: list[str], text: str) -> bool:
    if not args or args[0] != "push":
        return False
    low = text.lower()
    default_repo_low = DEFAULT_REPO.lower()
    return (
        ("permission to" in low and "denied" in low and default_repo_low in low)
        or ("write access to repository not granted" in low and default_repo_low in low)
        or ("remote: permission to" in low and "denied" in low and default_repo_low in low)
    )


def _org_join_instructions() -> str:
    return (
        "Looks like SSH key works, but you do not have write access to gravityhub-org/cuhkvoting-records.\n"
        "Please join GitHub organization 'gravityhub-org' to get repo write permission.\n"
        "Contact one of these maintainers to be added:\n"
        "- Samson Leong <samson32081@gmail.com>\n"
        "- Brian Hiu Yeung Cheng <1155175825@link.cuhk.edu.hk>\n"
        "- Hannuksela Otto Akseli <otto.akseli.hannuksela@gmail.com>"
    )


def _ssh_setup_instructions() -> str:
    return (
        "GitHub auth failed. Set up SSH key:\n"
        "1) ssh-keygen -t ed25519 -C \"you@example.com\"\n"
        "2) eval \"$(ssh-agent -s)\" && ssh-add ~/.ssh/id_ed25519\n"
        "3) Add ~/.ssh/id_ed25519.pub to GitHub SSH keys\n"
        "4) Test: ssh -T git@github.com"
    )


def _resolve_repo_config(args: SimpleNamespace) -> RepoConfig:
    owner, repo = DEFAULT_REPO.split("/", 1)
    repo_arg = args.repo or os.getenv("CUHKVOTING_REPO")
    if repo_arg:
        if "/" not in repo_arg:
            raise SystemExit(f"Repo must look like owner/name, and must be {DEFAULT_REPO}.")
        provided_owner, provided_repo = repo_arg.split("/", 1)
        if (provided_owner, provided_repo) != (owner, repo):
            raise SystemExit(f"Only {DEFAULT_REPO} is supported.")
    return RepoConfig(owner=owner, repo=repo, branch=args.branch)


def _has_github_ssh_access() -> bool:
    proc = subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", "git@github.com"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    return "successfully authenticated" in text


def _github_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_token() -> str | None:
    token = os.getenv("CUHKVOTING_TOKEN") or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        return token
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=5,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                return line[len("password="):].strip() or None
    except Exception:
        pass
    return None


def _get_user_from_token(token: str | None) -> str | None:
    if not token:
        return None
    try:
        data = _http_json("https://api.github.com/user", headers=_github_headers(token))
        user = data.get("login")
        return user if isinstance(user, str) else None
    except Exception:
        return None


def _load_paper_via_api(cfg: RepoConfig, path: str, token: str | None) -> tuple[dict | None, str | None]:
    url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}/contents/{path}?ref={cfg.branch}"
    try:
        data = _http_json(url, headers=_github_headers(token))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data.get("sha")


def _load_json_via_api(cfg: RepoConfig, path: str, token: str | None) -> tuple[dict | None, str | None]:
    return _load_paper_via_api(cfg, path, token)


def _save_paper_via_api(
    cfg: RepoConfig, path: str, paper: dict, sha: str | None, token: str, message: str
) -> None:
    url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}/contents/{path}"
    payload = {
        "message": message,
        "branch": cfg.branch,
        "content": base64.b64encode((json.dumps(paper, indent=2, sort_keys=True) + "\n").encode("utf-8")).decode(
            "ascii"
        ),
    }
    if sha:
        payload["sha"] = sha
    _http_json_request(url, "PUT", payload, headers=_github_headers(token))


def _save_json_via_api(cfg: RepoConfig, path: str, body: dict, sha: str | None, token: str, message: str) -> None:
    _save_paper_via_api(cfg, path, body, sha, token, message)


def _delete_paper_via_api(cfg: RepoConfig, path: str, sha: str, token: str, message: str) -> None:
    url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}/contents/{path}"
    payload = {"message": message, "branch": cfg.branch, "sha": sha}
    _http_json_request(url, "DELETE", payload, headers=_github_headers(token))


def _load_jc_records(cfg: RepoConfig, token: str | None) -> tuple[list[dict], str | None]:
    data, sha = _load_json_via_api(cfg, JC_RECORD_PATH, token)
    if data is not None:
        records = data.get("records", [])
        return (records if isinstance(records, list) else []), sha
    if _has_github_ssh_access():
        clone_dir = _with_repo_checkout(cfg)
        try:
            p = Path(clone_dir) / JC_RECORD_PATH
            if p.exists():
                body = json.loads(p.read_text(encoding="utf-8"))
                records = body.get("records", [])
                return (records if isinstance(records, list) else []), None
        finally:
            shutil.rmtree(clone_dir, ignore_errors=True)
    return [], None


def _save_jc_records(cfg: RepoConfig, token: str | None, user: str, records: list[dict], sha: str | None, message: str) -> None:
    body = {"records": records}
    if token:
        _save_json_via_api(cfg, JC_RECORD_PATH, body, sha, token, message)
        return
    if not _has_github_ssh_access():
        raise SystemExit(f"Writing records needs auth. Set CUHKVOTING_TOKEN/GITHUB_TOKEN or configure SSH key.\n\n{_ssh_setup_instructions()}")
    clone_dir = _with_repo_checkout(cfg)
    try:
        p = Path(clone_dir) / JC_RECORD_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _ensure_commit_identity(clone_dir, user)
        _run_git(["add", JC_RECORD_PATH], cwd=clone_dir)
        _run_git(["commit", "-m", message], cwd=clone_dir)
        _run_git(["push", "origin", f"HEAD:{cfg.branch}"], cwd=clone_dir)
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def _list_papers_via_graphql(cfg: RepoConfig, token: str) -> list[dict]:
    query = (
        '{ repository(owner: "%s", name: "%s") {'
        '  object(expression: "%s:papers") {'
        '    ... on Tree { entries { name object { ... on Blob { text } } } }'
        '  } } }'
    ) % (cfg.owner, cfg.repo, cfg.branch)
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query}).encode("utf-8"),
        headers={**_github_headers(token), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    entries = ((data.get("data") or {}).get("repository") or {}).get("object") or {}
    papers: list[dict] = []
    for entry in entries.get("entries", []):
        name = entry.get("name", "")
        if not name.endswith(".json") or name == "journal_club_records.json":
            continue
        text = (entry.get("object") or {}).get("text")
        if not text:
            continue
        try:
            paper = json.loads(text)
            if isinstance(paper, dict):
                papers.append(paper)
        except Exception:
            continue
    return papers


def _list_papers_via_api(cfg: RepoConfig, token: str | None) -> list[dict]:
    if token:
        try:
            return _list_papers_via_graphql(cfg, token)
        except (urllib.error.HTTPError, urllib.error.URLError,
                json.JSONDecodeError, KeyError, RuntimeError):
            pass
    url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}/git/trees/{cfg.branch}?recursive=1"
    try:
        data = _http_json(url, headers=_github_headers(token))
    except urllib.error.HTTPError as e:
        if e.code in (404, 409):
            return []
        raise
    papers: list[dict] = []
    for obj in data.get("tree", []):
        path = obj.get("path", "")
        if obj.get("type") != "blob" or not path.startswith("papers/") or not path.endswith(".json"):
            continue
        paper, _sha = _load_paper_via_api(cfg, path, token)
        if paper:
            papers.append(paper)
    return papers


def _arxiv_query(params: dict[str, str]) -> list[dict[str, str]]:
    url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
    delays = [10, 20, 60]
    for attempt, delay in enumerate([-1] + delays):
        if attempt > 0:
            typer.echo(f"arXiv rate limit, retrying in {delay}s…", err=True)
            time.sleep(delay)
        try:
            xml_str = _http_text(url, headers={"User-Agent": USER_AGENT})
            break
        except (ConnectionError, urllib.error.HTTPError, http.client.IncompleteRead) as e:
            code = getattr(e, "code", None)
            if attempt == len(delays) or (code is not None and code not in (429, 503)):
                raise
    root = ET.fromstring(xml_str)
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    entries: list[dict[str, str]] = []
    for ent in root.findall("atom:entry", ns):
        entry_id = (ent.findtext("atom:id", "", ns) or "").strip()
        title = " ".join((ent.findtext("atom:title", "", ns) or "").split())
        summary = " ".join((ent.findtext("atom:summary", "", ns) or "").split())
        authors: list[str] = []
        for author in ent.findall("atom:author", ns):
            full_name = " ".join((author.findtext("atom:name", "", ns) or "").split())
            if full_name:
                authors.append(full_name)
        arxiv_id = _strip_arxiv_version(entry_id.rsplit("/", 1)[-1])
        published = (ent.findtext("atom:published", "", ns) or "").strip()
        primary_cat_el = ent.find("arxiv:primary_category", ns)
        primary_category = primary_cat_el.get("term", "") if primary_cat_el is not None else ""
        entries.append(
            {
                "id": arxiv_id,
                "title": title,
                "abstract": summary,
                "url": f"{ARXIV_ABS}{arxiv_id}",
                "authors": authors,
                "published": published,
                "primary_category": primary_category,
            }
        )
    return entries


def _inspire_query(query: str, limit: int) -> list[dict[str, str]]:
    params = {
        "q": query,
        "size": str(limit),
        "sort": "mostrecent",
        "fields": "titles,abstracts,authors,arxiv_eprints,control_number",
    }
    url = f"{INSPIRE_API}?{urllib.parse.urlencode(params)}"
    data = _http_json(url, headers={"User-Agent": USER_AGENT})
    hits = data.get("hits", {}).get("hits", [])
    entries: list[dict[str, str]] = []
    for hit in hits if isinstance(hits, list) else []:
        metadata = hit.get("metadata", {}) if isinstance(hit, dict) else {}
        if not isinstance(metadata, dict):
            continue
        titles = metadata.get("titles", [])
        title = ""
        if isinstance(titles, list):
            for t in titles:
                if isinstance(t, dict) and isinstance(t.get("title"), str) and t.get("title", "").strip():
                    title = " ".join(t["title"].split())
                    break
        if not title:
            continue
        abstracts = metadata.get("abstracts", [])
        abstract = ""
        if isinstance(abstracts, list):
            for a in abstracts:
                if isinstance(a, dict) and isinstance(a.get("value"), str) and a.get("value", "").strip():
                    abstract = " ".join(a["value"].split())
                    break
        authors_raw = metadata.get("authors", [])
        authors: list[str] = []
        if isinstance(authors_raw, list):
            for author in authors_raw:
                if isinstance(author, dict):
                    name = author.get("full_name")
                    if isinstance(name, str) and name.strip():
                        authors.append(" ".join(name.split()))
        arxiv_id = ""
        arxiv_eprints = metadata.get("arxiv_eprints", [])
        if isinstance(arxiv_eprints, list):
            for ep in arxiv_eprints:
                if isinstance(ep, dict) and isinstance(ep.get("value"), str) and ep.get("value", "").strip():
                    arxiv_id = _strip_arxiv_version(ep["value"])
                    break
        if not arxiv_id:
            # Search output should stay arXiv-only.
            continue
        paper_id = arxiv_id
        paper_url = f"{ARXIV_ABS}{arxiv_id}"
        entries.append(
            {
                "id": paper_id,
                "title": title,
                "abstract": abstract,
                "url": paper_url,
                "authors": authors,
            }
        )
    return entries


def _build_inspire_title_query(keywords: list[str]) -> str:
    # Inspire free-text query can miss relevant hits; force per-token field query.
    stop = {"a", "an", "the", "of", "for", "to", "and", "or", "in", "on", "at", "by", "with"}
    tokens = [k.strip() for k in keywords if k.strip()]
    title_tokens = [t for t in tokens if t.lower() not in stop and len(t) > 1]
    if not title_tokens:
        title_tokens = tokens
    return " and ".join(f'(title:"{t}" or author:"{t}")' for t in title_tokens)


def _build_cat_query(categories: list[str]) -> str:
    if len(categories) == 1:
        return f"cat:{categories[0]}"
    return "(" + " OR ".join(f"cat:{c}" for c in categories) + ")"


def _entry_matches_any_category(entry: dict, categories: list[str]) -> bool:
    primary = entry.get("primary_category", "")
    return any(fnmatch.fnmatch(primary, pat) for pat in categories)


def _normalize_paper_id(raw_id: str) -> str:
    raw_id = raw_id.strip()
    if raw_id.startswith("http://") or raw_id.startswith("https://"):
        raw_id = raw_id.rstrip("/").rsplit("/", 1)[-1]
    return _strip_arxiv_version(raw_id.replace("arXiv:", ""))


def _safe_filename(paper_id: str) -> str:
    return paper_id.replace("/", "__")


def _format_clickable_id(arxiv_id: str) -> str:
    clean_id = _strip_arxiv_version(arxiv_id)
    url = f"{ARXIV_ABS}{clean_id}"
    # OSC 8 hyperlink: terminals without support still show raw id text.
    return f"\033]8;;{url}\033\\{clean_id}\033]8;;\033\\"


def _strip_arxiv_version(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", arxiv_id.strip())


def _last_name(full_name: str) -> str:
    # Keep suffix/punctuation simple; last token works well for arXiv names.
    parts = full_name.strip().split()
    return parts[-1] if parts else full_name.strip()


def _format_author_lastnames(authors: list[str], max_authors: int = 3) -> str:
    if not authors:
        return "unknown"
    chosen = [_last_name(a) for a in authors[:max_authors]]
    return ", ".join(chosen)


def _format_abstract(abstract: str, lines: int, wrap: int) -> str:
    if lines == 0 or not abstract:
        return ""
    wrapped = textwrap.wrap(abstract, width=wrap)
    chosen = wrapped if lines < 0 else wrapped[:lines]
    indent = "      "
    return "\n".join(indent + line for line in chosen)


def _author_matches_highlight(author: str, highlight: str) -> bool:
    """Match arXiv 'Firstname Lastname' against config 'Surname, Firstname'."""
    a = author.strip().lower()
    h = highlight.strip().lower()
    # Try rearranging "Surname, Firstname" → "Firstname Surname" for exact match
    if "," in h:
        surname, _, firstname = h.partition(",")
        if a == f"{firstname.strip()} {surname.strip()}":
            return True
    # Fallback: surname + initial — only when the arXiv firstname looks abbreviated
    # (single letter, optionally followed by a dot), e.g. "O. Hannuksela"
    parts = a.split()
    if len(parts) >= 2 and re.fullmatch(r"[a-z]\.?", parts[0]):
        surname = h.split(",")[0].strip()
        initial = parts[0].rstrip(".")
        config_firstname = h.partition(",")[2].strip()
        if " ".join(parts[1:]) == surname and config_firstname.lower().startswith(initial):
            return True
    return False


def _format_author_lastnames_highlighted(
    authors: list[str], max_authors: int, highlights: list[str]
) -> str:
    if not authors:
        return "unknown"
    parts = []
    for a in authors[:max_authors]:
        lastname = _last_name(a)
        if highlights and any(_author_matches_highlight(a, h) for h in highlights):
            parts.append(typer.style(lastname, fg=typer.colors.BRIGHT_BLUE, bold=True))
        else:
            parts.append(lastname)
    return ", ".join(parts)


_RE_CACHE: dict[str, re.Pattern | None] = {}


def _get_re(pattern: str) -> re.Pattern | None:
    if pattern not in _RE_CACHE:
        try:
            _RE_CACHE[pattern] = re.compile(pattern, re.IGNORECASE)
        except re.error:
            _RE_CACHE[pattern] = None
    return _RE_CACHE[pattern]


def _find_keyword_matches(texts: list[str], keywords: list[str]) -> list[str]:
    seen_lower: set[str] = set()
    results: list[str] = []
    for pattern in keywords:
        rx = _get_re(pattern)
        if rx is None:
            continue
        for text in texts:
            for m in rx.finditer(text):
                word = m.group(0)
                if word.lower() not in seen_lower:
                    seen_lower.add(word.lower())
                    results.append(word)
    return results


def _highlight_text(text: str, keywords: list[str]) -> str:
    spans: list[tuple[int, int]] = []
    for pattern in keywords:
        rx = _get_re(pattern)
        if rx is None:
            continue
        for m in rx.finditer(text):
            spans.append((m.start(), m.end()))
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + typer.style(text[start:end], fg=typer.colors.BRIGHT_BLUE, bold=True) + text[end:]
    return text


def _format_voters(votes: list[dict], table: dict[str, str] | None = None) -> str:
    voters = [_resolve_display_name(str(v.get("user", "")).strip(), table or {}) for v in votes]
    voters = [v for v in voters if v]
    if not voters:
        return "-"
    return ", ".join(sorted(set(voters), key=str.lower))


def _make_vote_entry(user: str) -> dict:
    return {"user": user, "voted_at": dt.datetime.utcnow().isoformat() + "Z"}


def _filter_entries(entries: list[dict[str, str]], keywords: list[str] | None) -> list[dict[str, str]]:
    if not keywords:
        return entries
    tokens = [k.strip().lower() for k in keywords if k.strip()]
    if not tokens:
        return entries
    filtered: list[dict[str, str]] = []
    for p in entries:
        hay = " ".join(
            [
                str(p.get("title", "")),
                str(p.get("abstract", "")),
                " ".join(p.get("authors", [])),
            ]
        ).lower()
        if all(t in hay for t in tokens):
            filtered.append(p)
    return filtered


def _parse_single_date(s: str) -> dt.date | None:
    """Parse YYYY-MM-DD or M-DD / MM-DD (most recent past occurrence). Returns None if not a date."""
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})-(\d{2})", s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        today = dt.date.today()
        try:
            candidate = dt.date(today.year, month, day)
        except ValueError:
            return None
        if candidate > today:
            candidate = dt.date(today.year - 1, month, day)
        return candidate
    return None


def _parse_date_token(s: str) -> tuple[dt.date, dt.date] | None:
    """Parse a date or A..B range into (start, end). Returns None if not a date token."""
    if ".." in s:
        parts = s.split("..", 1)
        a = _parse_single_date(parts[0])
        b = _parse_single_date(parts[1])
        if a and b:
            return (min(a, b), max(a, b))
        return None
    d = _parse_single_date(s)
    return (d, d) if d else None


def _entry_in_date_spans(entry: dict, spans: list[tuple[dt.date, dt.date]]) -> bool:
    pub = entry.get("published", "")[:10]
    try:
        d = dt.date.fromisoformat(pub)
    except ValueError:
        return False
    return any(s <= d <= e for s, e in spans)


def _find_legacy_paper_via_api(cfg: RepoConfig, base_id: str, token: str | None) -> tuple[dict | None, str | None, str | None]:
    url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}/git/trees/{cfg.branch}?recursive=1"
    try:
        data = _http_json(url, headers=_github_headers(token))
    except urllib.error.HTTPError as e:
        if e.code in (404, 409):
            return None, None, None
        raise
    for obj in data.get("tree", []):
        path = obj.get("path", "")
        if obj.get("type") != "blob" or not path.startswith("papers/") or not path.endswith(".json"):
            continue
        paper, sha = _load_paper_via_api(cfg, path, token)
        if paper and _strip_arxiv_version(str(paper.get("id", ""))) == base_id:
            return paper, sha, path
    return None, None, None


def _parse_utc(ts: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _latest_vote_timestamp(votes: list[dict]) -> float:
    """Max voted_at among votes as UTC unix time; 0 if none parseable."""
    best = 0.0
    for v in votes:
        d = _parse_utc(str(v.get("voted_at", "")))
        if d is None:
            continue
        ts = d.replace(tzinfo=dt.timezone.utc).timestamp()
        if ts > best:
            best = ts
    return best


def _prune_expired_votes(paper: dict) -> int:
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=VOTE_EXPIRY_DAYS)
    votes = paper.get("votes", [])
    kept = []
    for v in votes:
        voted_at = _parse_utc(str(v.get("voted_at", "")))
        if voted_at and voted_at >= cutoff:
            kept.append(v)
    removed = len(votes) - len(kept)
    paper["votes"] = kept
    return removed


def _resolve_user(token: str | None) -> str:
    user = os.getenv("CUHKVOTING_USER")
    if not user:
        user = os.getenv("GITHUB_USER")
    if not user:
        user = _get_user_from_token(token)
    if not user:
        try:
            user = _run_git(["config", "--global", "user.name"]).strip()
        except SystemExit:
            user = ""
    if not user:
        raise SystemExit("Could not identify user. Set CUHKVOTING_USER or configure git user.name.")
    return user


def _load_vote_paper(cfg: RepoConfig, token: str | None, paper_id: str) -> tuple[dict | None, str | None, str]:
    path = f"papers/{_safe_filename(paper_id)}.json"
    paper, sha = _load_paper_via_api(cfg, path, token)
    save_path = path
    if paper is None:
        legacy_paper, legacy_sha, legacy_path = _find_legacy_paper_via_api(cfg, paper_id, token)
        if legacy_paper is not None and legacy_path is not None:
            paper, sha, save_path = legacy_paper, legacy_sha, legacy_path
    if paper is None and token is None and _has_github_ssh_access():
        clone_dir = _with_repo_checkout(cfg)
        try:
            paper_file = Path(clone_dir) / path
            if paper_file.exists():
                paper = json.loads(paper_file.read_text(encoding="utf-8"))
            else:
                papers_dir = Path(clone_dir) / "papers"
                if papers_dir.exists():
                    for cand in papers_dir.glob("*.json"):
                        try:
                            cand_paper = json.loads(cand.read_text(encoding="utf-8"))
                        except Exception:
                            continue
                        if _strip_arxiv_version(str(cand_paper.get("id", ""))) == paper_id:
                            paper = cand_paper
                            save_path = str(cand.relative_to(clone_dir))
                            break
        finally:
            shutil.rmtree(clone_dir, ignore_errors=True)
    return paper, sha, save_path


def _save_vote_paper(
    cfg: RepoConfig,
    token: str | None,
    user: str,
    paper: dict,
    sha: str | None,
    save_path: str,
    message: str,
) -> None:
    if token:
        _save_paper_via_api(cfg, save_path, paper, sha, token, message)
        return
    if not _has_github_ssh_access():
        raise SystemExit(f"Voting needs auth. Set CUHKVOTING_TOKEN/GITHUB_TOKEN or configure SSH key.\n\n{_ssh_setup_instructions()}")
    clone_dir = _with_repo_checkout(cfg)
    try:
        paper_file = Path(clone_dir) / save_path
        paper_file.parent.mkdir(parents=True, exist_ok=True)
        paper_file.write_text(json.dumps(paper, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _ensure_commit_identity(clone_dir, user)
        _run_git(["add", str(Path(save_path))], cwd=clone_dir)
        _run_git(["commit", "-m", message], cwd=clone_dir)
        _run_git(["push", "origin", f"HEAD:{cfg.branch}"], cwd=clone_dir)
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def _delete_vote_paper(
    cfg: RepoConfig,
    token: str | None,
    user: str,
    save_path: str,
    sha: str | None,
    message: str,
) -> None:
    if token:
        if not sha:
            raise SystemExit("Cannot delete vote file via API: missing file SHA.")
        _delete_paper_via_api(cfg, save_path, sha, token, message)
        return
    if not _has_github_ssh_access():
        raise SystemExit(f"Voting needs auth. Set CUHKVOTING_TOKEN/GITHUB_TOKEN or configure SSH key.\n\n{_ssh_setup_instructions()}")
    clone_dir = _with_repo_checkout(cfg)
    try:
        paper_file = Path(clone_dir) / save_path
        if not paper_file.exists():
            raise SystemExit(f"Vote file not found for deletion: {save_path}")
        _ensure_commit_identity(clone_dir, user)
        _run_git(["rm", str(Path(save_path))], cwd=clone_dir)
        _run_git(["commit", "-m", message], cwd=clone_dir)
        _run_git(["push", "origin", f"HEAD:{cfg.branch}"], cwd=clone_dir)
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def _validate_arxiv_entry(paper_id: str) -> dict:
    entries = _arxiv_query({"search_query": f"id:{paper_id}", "start": "0", "max_results": "1"})
    if not entries:
        raise SystemExit(f"Could not find arXiv entry for id '{paper_id}'.")
    return entries[0]


def _with_repo_checkout(cfg: RepoConfig) -> str:
    tmpdir = tempfile.mkdtemp(prefix="cuhkvoting-")
    try:
        _run_git(["clone", "--depth", "1", "--branch", cfg.branch, cfg.ssh_clone_url, tmpdir])
        return tmpdir
    except SystemExit:
        shutil.rmtree(tmpdir, ignore_errors=True)
        tmpdir = tempfile.mkdtemp(prefix="cuhkvoting-")
        try:
            _run_git(["clone", "--depth", "1", cfg.ssh_clone_url, tmpdir])
            try:
                _run_git(["checkout", cfg.branch], cwd=tmpdir)
            except SystemExit:
                _run_git(["checkout", "-b", cfg.branch], cwd=tmpdir)
            return tmpdir
        except SystemExit:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise


def _ensure_commit_identity(repo_dir: str, user: str) -> None:
    try:
        email = _run_git(["config", "--global", "user.email"]).strip()
    except SystemExit:
        email = ""
    if not email:
        safe_user = re.sub(r"\s+", "-", user.strip().lower()) or "cuhkvoting-user"
        email = f"{safe_user}@users.noreply.github.com"
    _run_git(["config", "user.name", user], cwd=repo_dir)
    _run_git(["config", "user.email", email], cwd=repo_dir)


def _load_cache_data(key: str) -> dict | None:
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cache(key: str, categories: list[str], entries: list[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "fetched_at": dt.datetime.utcnow().isoformat() + "Z",
        "categories": sorted(categories),
        "entries": entries,
    }
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def _lookup_local_cache(paper_id: str) -> dict | None:
    for key in ("today", "lastweek"):
        data = _load_cache_data(key)
        if not data:
            continue
        for entry in data.get("entries", []):
            if _strip_arxiv_version(str(entry.get("id", ""))) == paper_id:
                return entry
    return None


def _resolve_cache(
    key: str,
    categories: list[str],
    max_age_seconds: int,
    fetch_fn,
) -> list[dict]:
    """Return entries, fetching and reconciling with category changes as needed."""
    data = _load_cache_data(key)

    if data is not None:
        fetched_at = _parse_utc(data.get("fetched_at", ""))
        is_stale = fetched_at is None or (dt.datetime.utcnow() - fetched_at).total_seconds() > max_age_seconds
        cached_cats = data.get("categories")
        if not is_stale and cached_cats:
            cached_set = set(cached_cats)
            current_set = set(categories)
            if cached_set == current_set:
                return list(data.get("entries", []))
            entries = list(data.get("entries", []))
            removed = cached_set - current_set
            added = current_set - cached_set
            if removed:
                entries = [e for e in entries if not _entry_matches_any_category(e, list(removed))]
            if added:
                new_entries = fetch_fn(list(added))
                existing_ids = {e["id"] for e in entries}
                entries += [e for e in new_entries if e["id"] not in existing_ids]
            _save_cache(key, categories, entries)
            return entries

    entries = fetch_fn(categories)
    _save_cache(key, categories, entries)
    return entries


def _load_last_list() -> list[dict]:
    if not LAST_LIST_PATH.exists():
        return []
    try:
        return json.loads(LAST_LIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_last_list(entries: list[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_LIST_PATH.write_text(
        json.dumps([{"id": e["id"], "title": e.get("title", "")} for e in entries], indent=2),
        encoding="utf-8",
    )


def _fetch_display_names(cfg: RepoConfig, token: str | None) -> dict[str, str]:
    """Load the display_names.json table from the repo. Returns {} if absent."""
    try:
        data, _ = _load_paper_via_api(cfg, DISPLAY_NAMES_PATH, token)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _resolve_display_name(user: str, table: dict[str, str]) -> str:
    return table.get(user) or user


def _warn_if_display_name_changed(current: str) -> None:
    """Warn once when display_name differs from the last recorded value."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    previous = DISPLAY_NAME_CACHE.read_text(encoding="utf-8").strip() if DISPLAY_NAME_CACHE.exists() else None
    DISPLAY_NAME_CACHE.write_text(current, encoding="utf-8")
    if previous is None or previous == current:
        return
    if previous:
        typer.echo(
            typer.style(
                f"Note: display name changed ('{previous}' → '{current}'). "
                "The shared table will be updated with your next vote.",
                fg=typer.colors.YELLOW,
            )
        )
    else:
        typer.echo(
            typer.style(
                f"Note: display name set to '{current}'. "
                "The shared table will be updated with your next vote.",
                fg=typer.colors.YELLOW,
            )
        )


def _resolve_paper_ids(raw_ids: list[str]) -> list[tuple[str, str | None, int | None]]:
    """Resolve raw IDs or 1-based integer indices to (arxiv_id, title_or_none, index_or_none) triples.

    index_or_none and title are non-None only when the entry came from an integer index.
    Raises SystemExit with a red error message if any index is out of range.
    All indices are validated before any IDs are returned.
    """
    last_list: list[dict] = _load_last_list()
    last_list_by_id = {_normalize_paper_id(e["id"]): e.get("title") or "" for e in last_list}
    results: list[tuple[str, str | None, int | None]] = []
    errors: list[str] = []

    for r in raw_ids:
        s = r.strip()
        try:
            idx = int(s)
            is_int = True
        except ValueError:
            is_int = False

        if is_int:
            if not last_list or idx < 1 or idx > len(last_list):
                n = len(last_list)
                errors.append(f"Index {idx} out of range (last list has {n} entries).")
            else:
                entry = last_list[idx - 1]
                results.append((_normalize_paper_id(entry["id"]), entry.get("title") or "", idx))
        else:
            arxiv_id = _normalize_paper_id(s)
            title = last_list_by_id.get(arxiv_id)
            results.append((arxiv_id, title, None))

    if errors:
        for err in errors:
            typer.echo(typer.style(f"Error: {err}", fg=typer.colors.RED), err=True)
        raise SystemExit(1)

    return results


def _fetch_today_entries(categories: list[str]) -> list[dict]:
    now = dt.datetime.utcnow()
    start = (now - dt.timedelta(days=1)).strftime("%Y%m%d%H%M")
    end = now.strftime("%Y%m%d%H%M")
    return _arxiv_query({
        "search_query": f"{_build_cat_query(categories)} AND submittedDate:[{start} TO {end}]",
        "start": "0",
        "max_results": "200",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })


def _fetch_lastweek_entries(categories: list[str]) -> list[dict]:
    end_day = dt.datetime.utcnow().date()
    start_day = end_day - dt.timedelta(days=7)
    return _arxiv_query({
        "search_query": f"{_build_cat_query(categories)} AND submittedDate:[{start_day.strftime('%Y%m%d')}0000 TO {end_day.strftime('%Y%m%d')}2359]",
        "start": "0",
        "max_results": "1000",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })


def _fetch_daterange_entries(start: dt.date, end: dt.date, categories: list[str]) -> list[dict]:
    return _arxiv_query({
        "search_query": (
            f"{_build_cat_query(categories)} AND "
            f"submittedDate:[{start.strftime('%Y%m%d')}0000 TO {end.strftime('%Y%m%d')}2359]"
        ),
        "start": "0",
        "max_results": "500",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })


def _parse_categories(args_category: list[str] | None, default: list[str]) -> list[str]:
    if args_category:
        return [c.strip() for item in args_category for c in item.split(",")]
    return default


def _format_kw_suffix(matches: list[str], count: int, glyph: str) -> str:
    if not matches:
        return ""
    if count == 0:
        return typer.style(f" {glyph} × {len(matches)}", fg=typer.colors.BRIGHT_BLUE, bold=True)
    shown = matches if count < 0 else matches[:count]
    parts = ", ".join(shown)
    if count >= 1 and len(matches) > count:
        parts += f", {len(matches) - count}+"
    return typer.style(f" [{parts}]", fg=typer.colors.BRIGHT_BLUE, bold=True)


def _apply_keyword_highlights(abstract: str, keywords: list[str]) -> str:
    lines_out = []
    for line in abstract.splitlines():
        indent = len(line) - len(line.lstrip())
        lines_out.append(line[:indent] + _highlight_text(line[indent:], keywords))
    return "\n".join(lines_out)


def _update_dn_table(dn_table: dict, user: str, display_name: str) -> bool:
    if display_name:
        if dn_table.get(user) != display_name:
            dn_table[user] = display_name
            return True
    elif user in dn_table:
        del dn_table[user]
        return True
    return False


def _print_entry_list(
    entries: list[dict],
    cfg: Config,
    abstract_lines: int,
    highlight_kw_count: int,
) -> None:
    for idx, p in enumerate(entries, 1):
        authors = p.get("authors", [])
        lastnames = _format_author_lastnames_highlighted(authors, 3, cfg.highlight_authors)
        title = p.get("title", "")
        abstract_text = p.get("abstract", "")
        kw_suffix = ""
        if cfg.highlight_keywords:
            matches = _find_keyword_matches([title, abstract_text], cfg.highlight_keywords)
            kw_suffix = _format_kw_suffix(matches, highlight_kw_count, cfg.highlight_glyph)
        print(f"{idx:>2}. {_format_clickable_id(p['id'])}  {title}  [{lastnames}]{kw_suffix}")
        abstract = _format_abstract(abstract_text, abstract_lines, cfg.abstract_wrap)
        if abstract and cfg.highlight_keywords:
            abstract = _apply_keyword_highlights(abstract, cfg.highlight_keywords)
        if abstract:
            print(abstract)


def cmd_today(args: SimpleNamespace) -> int:
    cfg = _load_config()
    categories = _parse_categories(getattr(args, "category", None), cfg.categories)
    max_age_minutes = getattr(args, "max_age", None)
    max_age_seconds = (int(max_age_minutes) if max_age_minutes is not None else cfg.today_max_age) * 60
    abstract_lines = getattr(args, "abstract", None)
    if abstract_lines is None:
        abstract_lines = cfg.abstract_lines
    entries = _resolve_cache("today", categories, max_age_seconds, _fetch_today_entries)
    if not entries:
        print("No papers found for today (UTC).")
        return 0
    entries = _filter_entries(entries, getattr(args, "keywords", None))
    if not entries:
        print("No papers matched keyword filter.")
        return 0
    displayed = entries[: int(args.limit)]
    _save_last_list(displayed)
    highlight_kw_count = getattr(args, "highlight_keywords", None)
    if highlight_kw_count is None:
        highlight_kw_count = cfg.highlight_keyword_count
    _print_entry_list(displayed, cfg, abstract_lines, highlight_kw_count)
    return 0


def cmd_search(args: SimpleNamespace) -> int:
    query_parts = [str(q).strip() for q in (args.query or []) if str(q).strip()]
    if not query_parts:
        raise SystemExit("Search query cannot be empty.")
    query = _build_inspire_title_query(query_parts)
    requested_limit = int(args.limit)
    fetch_limit = min(max(requested_limit * 5, 100), 500)
    entries = _inspire_query(query, fetch_limit)
    entries = _filter_entries(entries, query_parts)
    if not entries:
        print("No matches.")
        return 0
    displayed = entries[:requested_limit]
    _save_last_list(displayed)
    for idx, p in enumerate(displayed, 1):
        pid = str(p.get("id", ""))
        print(f"{idx:>2}. {_format_clickable_id(pid)}  {p['title']}")
    return 0


def cmd_lastweek(args: SimpleNamespace) -> int:
    cfg = _load_config()
    categories = _parse_categories(getattr(args, "category", None), cfg.categories)
    max_age_minutes = getattr(args, "max_age", None)
    max_age_seconds = (int(max_age_minutes) if max_age_minutes is not None else cfg.lastweek_max_age) * 60
    entries = _resolve_cache("lastweek", categories, max_age_seconds, _fetch_lastweek_entries)
    # Opportunistically seed today's cache from lastweek data if today's is stale
    today_data = _load_cache_data("today")
    today_fetched_at = _parse_utc(today_data.get("fetched_at", "")) if today_data else None
    today_stale = (
        today_data is None
        or today_fetched_at is None
        or (dt.datetime.utcnow() - today_fetched_at).total_seconds() > cfg.today_max_age * 60
        or set(today_data.get("categories", [])) != set(categories)
    )
    if today_stale:
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=1)
        today_entries = [
            e for e in entries
            if (_parse_utc(e.get("published", "")) or dt.datetime.min) >= cutoff
        ]
        _save_cache("today", categories, today_entries)
    abstract_lines = getattr(args, "abstract", None)
    if abstract_lines is None:
        abstract_lines = cfg.abstract_lines
    entries = _filter_entries(entries, getattr(args, "keywords", None))
    if not entries:
        print("No entries matched in last week (UTC).")
        return 0
    displayed = entries[: int(args.limit)]
    _save_last_list(displayed)
    highlight_kw_count = getattr(args, "highlight_keywords", None)
    if highlight_kw_count is None:
        highlight_kw_count = cfg.highlight_keyword_count
    _print_entry_list(displayed, cfg, abstract_lines, highlight_kw_count)
    return 0


def _topvoted_rows_from_papers(papers: list[dict], dn_table: dict[str, str]) -> list[dict]:
    """Build sorted rows for `topvoted`. Mutates each paper via `_prune_expired_votes`."""
    rows: list[dict] = []
    for paper in papers:
        _prune_expired_votes(paper)
        if paper.get("selected"):
            continue
        votes = paper.get("votes", [])
        vote_count = len(votes)
        if vote_count == 0:
            continue
        rows.append(
            {
                "id": _strip_arxiv_version(str(paper.get("id", "(unknown)"))),
                "title": " ".join(paper.get("title", "(no title)").split()),
                "abstract": " ".join(paper.get("abstract", "").split()),
                "votes": vote_count,
                "voters": _format_voters(votes, dn_table),
                "latest_vote_ts": _latest_vote_timestamp(votes),
            }
        )
    rows.sort(key=lambda p: (-p["votes"], -p["latest_vote_ts"], p["id"]))
    return rows


def cmd_topvoted(args: SimpleNamespace) -> int:
    cfg = _load_config()
    repo_cfg = _resolve_repo_config(args)
    token = _get_token()
    papers = _list_papers_via_api(repo_cfg, token)
    if not papers and _has_github_ssh_access():
        clone_dir = _with_repo_checkout(repo_cfg)
        try:
            papers_dir = Path(clone_dir) / "papers"
            for path in sorted(papers_dir.glob("*.json")) if papers_dir.exists() else []:
                try:
                    papers.append(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
        finally:
            shutil.rmtree(clone_dir, ignore_errors=True)

    dn_table = _fetch_display_names(repo_cfg, token)
    rows = _topvoted_rows_from_papers(papers, dn_table)
    topn = rows[: args.N]
    if not topn:
        print("No voted papers yet.")
        return 0
    _save_last_list(topn)

    abstract_lines = getattr(args, "abstract", None)
    if abstract_lines is None:
        abstract_lines = cfg.abstract_lines

    # Group consecutive papers by vote count
    groups: list[list[dict]] = []
    for p in topn:
        if groups and groups[-1][0]["votes"] == p["votes"]:
            groups[-1].append(p)
        else:
            groups.append([p])

    has_ties = any(len(g) > 1 for g in groups)
    w = len(str(len(topn)))

    idx = 1
    for group in groups:
        for k, p in enumerate(group):
            is_first = k == 0
            is_last = k == len(group) - 1
            grey = not is_first

            if has_ties:
                if len(group) == 1:
                    box = " "
                elif is_first:
                    box = "┳"
                elif is_last:
                    box = "┗"
                else:
                    box = "┣"
                abs_prefix = f"{' ' * (w + 2)}┃ " if not is_last else f"{' ' * (w + 4)}"
            else:
                box = ""
                abs_prefix = f"{' ' * (w + 2)}"

            idx_str = typer.style(f"{idx:>{w}}.", fg=typer.colors.BRIGHT_BLACK) if grey else f"{idx:>{w}}."
            n = p["votes"]
            vote_label = f"{n} vote" + ("s" if n != 1 else "")
            voters_str = typer.style(f"[{p['voters']}] ⇒ {vote_label}", fg=typer.colors.CYAN)
            if has_ties:
                print(f"{idx_str} {box} {_format_clickable_id(p['id'])}  {p['title']}  {voters_str}")
            else:
                print(f"{idx_str} {_format_clickable_id(p['id'])}  {p['title']}  {voters_str}")

            if abstract_lines:
                wrapped = textwrap.wrap(p.get("abstract", ""), width=cfg.abstract_wrap)
                shown = wrapped if abstract_lines < 0 else wrapped[:abstract_lines]
                for al in shown:
                    print(abs_prefix + al)

            idx += 1
    return 0


def cmd_show(args: SimpleNamespace) -> int:
    cfg = _load_config()
    resolved = _resolve_paper_ids(args.paper_ids)
    for i, (arxiv_id, _title, _idx) in enumerate(resolved):
        entry = _lookup_local_cache(arxiv_id)
        if entry is None:
            entry = _validate_arxiv_entry(arxiv_id)
        authors = entry.get("authors", [])
        lastnames = _format_author_lastnames_highlighted(authors, 3, cfg.highlight_authors)
        title = entry.get("title", "")
        abstract_text = entry.get("abstract", "")
        kw_suffix = ""
        if cfg.highlight_keywords:
            matches = _find_keyword_matches([title, abstract_text], cfg.highlight_keywords)
            kw_suffix = _format_kw_suffix(matches, cfg.highlight_keyword_count, cfg.highlight_glyph)
        if i > 0:
            print()
        print(f"{_format_clickable_id(arxiv_id)}  {title}  [{lastnames}]{kw_suffix}")
        abstract = _format_abstract(abstract_text, -1, cfg.abstract_wrap)
        if abstract and cfg.highlight_keywords:
            abstract = _apply_keyword_highlights(abstract, cfg.highlight_keywords)
        if abstract:
            print(abstract)
    return 0


def cmd_show_date(args: SimpleNamespace) -> int:
    """Show papers for a specific date or date range, numbered for index-based vote."""
    date_spans: list[tuple[dt.date, dt.date]] = args.date_spans
    start = min(s for s, _ in date_spans)
    end   = max(e for _, e in date_spans)

    today = dt.date.today()
    if end > today:
        raise SystemExit("Date is in the future.")
    if start < ARXIV_FOUNDING_DATE:
        raise SystemExit("arXiv was founded on 1991-08-14.")

    cfg = _load_config()
    categories = _parse_categories(getattr(args, "categories", None), cfg.categories)
    abstract_lines = getattr(args, "abstract", None)
    if abstract_lines is None:
        abstract_lines = cfg.abstract_lines

    cache_key = f"date-{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
    all_entries = _resolve_cache(
        cache_key, categories, 999_999_999,
        lambda cats: _fetch_daterange_entries(start, end, cats),
    )

    entries = [e for e in all_entries if _entry_in_date_spans(e, date_spans)]
    entries = _filter_entries(entries, getattr(args, "keywords", None) or [])

    if not entries:
        print("No papers found for the requested date(s).")
        return 0

    displayed = entries[: int(getattr(args, "limit", 200))]
    _save_last_list(displayed)

    # Header
    def _span_label(s: dt.date, e: dt.date) -> str:
        return s.isoformat() if s == e else f"{s.isoformat()}..{e.isoformat()}"
    header_label = ", ".join(_span_label(s, e) for s, e in date_spans)
    print(f"Papers for {header_label} ({len(displayed)} entries):")

    highlight_kw_count = getattr(args, "highlight_keywords", None)
    if highlight_kw_count is None:
        highlight_kw_count = cfg.highlight_keyword_count
    _print_entry_list(displayed, cfg, abstract_lines, highlight_kw_count)
    return 0


def _batch_vote_papers_ssh(
    cfg: RepoConfig,
    user: str,
    papers: list[dict],
    display_name: str = "",
) -> list[str]:
    """Vote for multiple papers in a single clone/commit/push cycle.

    Each dict in `papers` must have: paper_id, title, url.
    Returns the list of paper_ids for which a vote was recorded.
    """
    if not papers:
        return []
    clone_dir = _with_repo_checkout(cfg)
    voted: list[str] = []
    new_votes: list[str] = []
    try:
        papers_dir = Path(clone_dir) / "papers"
        papers_dir.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.utcnow().isoformat() + "Z"
        for p in papers:
            paper_id = _strip_arxiv_version(p["paper_id"])
            title = p.get("title", "")
            url = p.get("url", f"{ARXIV_ABS}{paper_id}")
            paper_file = papers_dir / f"{_safe_filename(paper_id)}.json"
            # No individual API GET — files are read directly from the local checkout,
            # preserving any existing votes from other users without extra round-trips.
            if paper_file.exists():
                try:
                    paper = json.loads(paper_file.read_text(encoding="utf-8"))
                except Exception:
                    paper = {"id": paper_id, "title": title, "abstract": "", "url": url, "votes": []}
            else:
                paper = {"id": paper_id, "title": title, "abstract": "", "url": url, "votes": []}
            _prune_expired_votes(paper)
            votes = paper.setdefault("votes", [])
            if any(v.get("user") == user for v in votes):
                print(f"Already voted: {user} -> {paper_id}")
                voted.append(paper_id)
                continue
            paper["id"] = paper_id
            votes.append(_make_vote_entry(user))
            paper_file.write_text(json.dumps(paper, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            voted.append(paper_id)
            new_votes.append(paper_id)
        # Update display_names.json if the user's entry has changed
        dn_file = Path(clone_dir) / DISPLAY_NAMES_PATH
        dn_table: dict = {}
        if dn_file.exists():
            try:
                dn_table = json.loads(dn_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        dn_changed = _update_dn_table(dn_table, user, display_name)
        if dn_changed:
            dn_file.write_text(json.dumps(dn_table, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        if new_votes or dn_changed:
            _ensure_commit_identity(clone_dir, user)
            if new_votes:
                _run_git(["add", "papers/"], cwd=clone_dir)
            if dn_changed:
                _run_git(["add", DISPLAY_NAMES_PATH], cwd=clone_dir)
            if new_votes:
                ids_str = ", ".join(new_votes[:3]) + ("…" if len(new_votes) > 3 else "")
                msg = f"vote: {user} -> [{ids_str}] ({len(new_votes)} papers)"
            else:
                msg = f"display-name: update {user}"
            _run_git(["commit", "-m", msg], cwd=clone_dir)
            _run_git(["push", "origin", f"HEAD:{cfg.branch}"], cwd=clone_dir)
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)
    return voted


def _git_batch_commit(
    base_url: str,
    json_headers: dict,
    token: str,
    branch: str,
    tree_entries: list[dict],
    commit_msg: str,
) -> None:
    """Read HEAD → create tree → create commit → PATCH ref. Retries once on 422 (concurrent push)."""
    for _attempt in range(2):
        ref_data = _http_json(
            f"{base_url}/git/refs/heads/{branch}", headers=_github_headers(token)
        )
        head_sha = ref_data["object"]["sha"]
        commit_data = _http_json(
            f"{base_url}/git/commits/{head_sha}", headers=_github_headers(token)
        )
        base_tree_sha = commit_data["tree"]["sha"]
        tree_resp = _http_json_request(
            f"{base_url}/git/trees",
            "POST",
            {"base_tree": base_tree_sha, "tree": tree_entries},
            headers=json_headers,
        )
        commit_resp = _http_json_request(
            f"{base_url}/git/commits",
            "POST",
            {"message": commit_msg, "tree": tree_resp["sha"], "parents": [head_sha]},
            headers=json_headers,
        )
        try:
            _http_json_request(
                f"{base_url}/git/refs/heads/{branch}",
                "PATCH",
                {"sha": commit_resp["sha"], "force": False},
                headers=json_headers,
            )
            return
        except urllib.error.HTTPError as e:
            if e.code == 422 and _attempt == 0:
                continue  # re-read HEAD and retry
            try:
                body = json.loads(e.read().decode("utf-8"))
                msg = body.get("message", e.reason)
            except Exception:
                msg = e.reason
            raise RuntimeError(f"GitHub ref update failed (HTTP {e.code}): {msg}") from e


def _batch_vote_papers_api(
    cfg: RepoConfig,
    token: str,
    user: str,
    papers: list[dict],
    display_name: str = "",
) -> list[str]:
    """Vote for multiple papers in a single Git commit via the GitHub Git Data API.

    Each dict in `papers` must have: paper_id, title, url.
    Returns the list of paper_ids for which a vote was recorded (including already-voted).
    Reduces API calls from 2N sequential to N parallel blob POSTs + 5 overhead calls.
    """
    if not papers:
        return []

    base_url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}"
    json_headers = {**_github_headers(token), "Content-Type": "application/json"}
    ts = dt.datetime.utcnow().isoformat() + "Z"

    # Step 1: fetch all needed paper files + display_names.json in one GraphQL request
    aliases: list[str] = []
    alias_map: dict[str, tuple[str, str, dict]] = {}  # alias -> (paper_id, path, input_dict)
    for i, p in enumerate(papers):
        paper_id = _strip_arxiv_version(p["paper_id"])
        path = f"papers/{_safe_filename(paper_id)}.json"
        alias = f"p{i}"
        aliases.append(
            f'{alias}: object(expression: "{cfg.branch}:{path}") {{ ... on Blob {{ text }} }}'
        )
        alias_map[alias] = (paper_id, path, p)
    aliases.append(
        f'dn: object(expression: "{cfg.branch}:{DISPLAY_NAMES_PATH}") {{ ... on Blob {{ text }} }}'
    )

    query = '{ repository(owner: "%s", name: "%s") { %s } }' % (
        cfg.owner, cfg.repo, " ".join(aliases)
    )
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query}).encode("utf-8"),
        headers=json_headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        gql_data = json.loads(resp.read().decode("utf-8"))

    repo_node = ((gql_data.get("data") or {}).get("repository") or {})

    # Step 2: apply votes to each paper dict
    updates: list[tuple[str, str, dict]] = []  # (path, paper_id, updated_paper)
    voted: list[str] = []

    for alias, (paper_id, path, p) in alias_map.items():
        title = p.get("title", "")
        url = p.get("url", f"{ARXIV_ABS}{paper_id}")
        text = (repo_node.get(alias) or {}).get("text")
        if text:
            try:
                paper = json.loads(text)
            except Exception:
                paper = {"id": paper_id, "title": title, "abstract": "", "url": url, "votes": []}
        else:
            paper = {"id": paper_id, "title": title, "abstract": "", "url": url, "votes": []}

        _prune_expired_votes(paper)
        votes = paper.setdefault("votes", [])
        if any(v.get("user") == user for v in votes):
            print(f"Already voted: {user} -> {paper_id}")
            voted.append(paper_id)
            continue

        paper["id"] = paper_id
        votes.append(_make_vote_entry(user))
        updates.append((path, paper_id, paper))
        voted.append(paper_id)

    # Check if display_names.json needs updating
    dn_text = (repo_node.get("dn") or {}).get("text")
    dn_table: dict[str, str] = {}
    if dn_text:
        try:
            dn_table = json.loads(dn_text)
        except Exception:
            pass
    dn_changed = _update_dn_table(dn_table, user, display_name)
    if dn_changed:
        updates.append((DISPLAY_NAMES_PATH, "__dn__", dn_table))

    if not updates:
        return voted

    # Step 3: POST blobs concurrently (one per updated file)
    def _post_blob(path_paper: tuple[str, str, dict]) -> tuple[str, str]:
        path, _pid, paper = path_paper
        content = base64.b64encode(
            (json.dumps(paper, indent=2, sort_keys=True) + "\n").encode("utf-8")
        ).decode("ascii")
        resp = _http_json_request(
            f"{base_url}/git/blobs",
            "POST",
            {"content": content, "encoding": "base64"},
            headers=json_headers,
        )
        return path, resp["sha"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(updates))) as ex:
        blob_results = list(ex.map(_post_blob, updates))

    new_ids = [pid for _, pid, _ in updates if pid != "__dn__"]
    ids_str = ", ".join(new_ids[:3]) + ("…" if len(new_ids) > 3 else "")
    commit_msg = (
        f"vote: {user} -> [{ids_str}] ({len(new_ids)} papers)"
        if new_ids
        else f"display-name: update {user}"
    )
    tree_entries = [
        {"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}
        for path, blob_sha in blob_results
    ]
    _git_batch_commit(base_url, json_headers, token, cfg.branch, tree_entries, commit_msg)
    return voted


def _vote_paper_with_metadata(
    cfg: RepoConfig,
    token: str | None,
    user: str,
    paper_id: str,
    title: str,
    url: str,
    display_name: str = "",
) -> int:
    """Vote for a paper using pre-known metadata, skipping arXiv validation."""
    paper, sha, save_path = _load_vote_paper(cfg, token, paper_id)
    if paper is None:
        paper = {"id": paper_id, "title": title, "abstract": "", "url": url, "votes": []}
    _prune_expired_votes(paper)
    votes = paper.setdefault("votes", [])
    if any(v.get("user") == user for v in votes):
        print(f"Already voted: {user} -> {paper_id}")
        return 0
    paper["id"] = _strip_arxiv_version(str(paper.get("id", paper_id)))
    votes.append(_make_vote_entry(user))
    _save_vote_paper(cfg, token, user, paper, sha, save_path, f"vote: {user} -> {paper['id']}")
    print(f"Vote recorded: {user} -> {paper['id']}")
    return 0


def cmd_vote(args: SimpleNamespace) -> int:
    app_cfg = _load_config()
    display_name = getattr(args, "display_name", None) or app_cfg.display_name
    cfg = _resolve_repo_config(args)
    token = getattr(args, "token", None) or _get_token()
    user = getattr(args, "user", None) or _resolve_user(token)
    paper_id = _normalize_paper_id(args.paper_id)
    paper, sha, save_path = _load_vote_paper(cfg, token, paper_id)

    if paper is None:
        cached = _lookup_local_cache(paper_id)
        entry = cached if cached else _validate_arxiv_entry(paper_id)
        paper = {
            "id": entry["id"],
            "title": entry.get("title", ""),
            "abstract": entry.get("abstract", ""),
            "url": entry.get("url", f"{ARXIV_ABS}{paper_id}"),
            "votes": [],
        }
    elif not paper.get("abstract"):
        cached = _lookup_local_cache(paper_id)
        if cached and cached.get("abstract"):
            paper["abstract"] = cached["abstract"]
        elif not cached:
            entry = _validate_arxiv_entry(paper_id)
            paper["abstract"] = entry.get("abstract", "")

    _prune_expired_votes(paper)
    votes = paper.setdefault("votes", [])
    if any(v.get("user") == user for v in votes):
        raise SystemExit(f"User '{user}' already voted for {paper.get('id', paper_id)}.")
    paper_vote_id = _strip_arxiv_version(str(paper.get("id", paper_id)))
    paper["id"] = paper_vote_id
    votes.append(_make_vote_entry(user))
    _save_vote_paper(cfg, token, user, paper, sha, save_path, f"vote: {user} -> {paper_vote_id}")
    print(f"Vote recorded: {user} -> {paper_vote_id}")
    return 0


def cmd_vote_remove(args: SimpleNamespace) -> int:
    cfg = _resolve_repo_config(args)
    token = _get_token()
    user = _resolve_user(token)
    paper_id = _normalize_paper_id(args.paper_id)
    paper, sha, save_path = _load_vote_paper(cfg, token, paper_id)
    if paper is None:
        raise SystemExit(f"No vote record found for '{paper_id}'.")
    _prune_expired_votes(paper)
    votes = paper.setdefault("votes", [])
    kept = [v for v in votes if v.get("user") != user]
    if len(kept) == len(votes):
        raise SystemExit(f"User '{user}' has no active vote for {paper_id}.")
    paper["votes"] = kept
    if not paper["votes"] and not paper.get("selected"):
        _delete_vote_paper(cfg, token, user, save_path, sha, f"vote-remove: delete empty {paper_id}")
        print(f"Vote removed and record deleted: {user} -> {paper_id}")
        return 0
    _save_vote_paper(cfg, token, user, paper, sha, save_path, f"vote-remove: {user} -> {paper_id}")
    print(f"Vote removed: {user} -> {paper_id}")
    return 0


def cmd_select(args: SimpleNamespace) -> int:
    cfg = _resolve_repo_config(args)
    token = _get_token()
    user = _resolve_user(token)
    paper_id = _normalize_paper_id(args.paper_id)
    entry = _validate_arxiv_entry(paper_id)
    paper, sha, save_path = _load_vote_paper(cfg, token, paper_id)
    if paper is None:
        paper = {
            "id": entry["id"],
            "title": entry["title"],
            "abstract": entry["abstract"],
            "url": entry["url"],
            "votes": [],
        }
    _prune_expired_votes(paper)
    vote_count = len(paper.get("votes", []))
    now = dt.datetime.utcnow()
    year, week, _ = now.isocalendar()
    week_tag = f"{year}-W{week:02d}"
    selected_at = now.isoformat() + "Z"
    canonical_id = _strip_arxiv_version(str(paper.get("id", paper_id)))

    records, record_sha = _load_jc_records(cfg, token)
    records.append(
        {
            "week": week_tag,
            "arxiv_id": canonical_id,
            "title": paper.get("title", entry["title"]),
            "historical_vote": vote_count,
            "selected_at": selected_at,
            "selected_by": user,
        }
    )
    _save_jc_records(cfg, token, user, records, record_sha, f"record-select: {user} -> {canonical_id} ({week_tag})")

    if paper is not None and sha is not None:
        _delete_vote_paper(cfg, token, user, save_path, sha, f"select-delete-vote: {user} -> {canonical_id}")
    elif paper is not None and _has_github_ssh_access():
        # If loaded via git fallback without sha, still delete through git path.
        _delete_vote_paper(cfg, None, user, save_path, None, f"select-delete-vote: {user} -> {canonical_id}")

    print(f"Selected for presentation: {canonical_id} ({week_tag}) by {user}")
    return 0


def cmd_admin_trash(args: SimpleNamespace) -> int:
    cfg = _resolve_repo_config(args)
    token = _get_token()
    user = _resolve_user(token)
    paper_id = _normalize_paper_id(args.vote_id)
    paper, sha, save_path = _load_vote_paper(cfg, token, paper_id)
    if paper is None:
        raise SystemExit(f"No vote record found for '{paper_id}'.")
    _delete_vote_paper(cfg, token, user, save_path, sha, f"admin-trash: {user} -> {paper_id}")
    print(f"Trashed and deleted vote record: {paper_id}")
    return 0


def cmd_record(args: SimpleNamespace) -> int:
    cfg = _resolve_repo_config(args)
    token = _get_token()
    records, _sha = _load_jc_records(cfg, token)
    if not records:
        print("No journal club records yet.")
        return 0
    rows = sorted(records, key=lambda r: str(r.get("selected_at", "")), reverse=True)
    for i, r in enumerate(rows, 1):
        week = str(r.get("week", "?"))
        arxiv_id = str(r.get("arxiv_id", "?"))
        title = str(r.get("title", "(no title)"))
        hist = int(r.get("historical_vote", 0))
        selected_by = str(r.get("selected_by", "?"))
        print(f"{i:>2}. {week}  {_format_clickable_id(arxiv_id)}  votes:{hist}  by:{selected_by}  {title}")
    return 0


app = typer.Typer(
    name="cuhkvoting",
    help="Minimal arXiv voting CLI backed by GitHub.",
    add_completion=True,
)


def _invoke_cmd(func, **kwargs: object) -> int:
    """Run CLI command-like func; return exit code (does not terminate process)."""
    args = SimpleNamespace(**kwargs)
    try:
        code = func(args)
        return int(code)
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="ignore")
        typer.echo(f"HTTP {e.code}: {msg[:300]}", err=True)
        return 1
    except urllib.error.URLError as e:
        typer.echo(f"Network error: {e.reason}", err=True)
        return 1
    except ConnectionError as e:
        typer.echo(
            f"arXiv closed the connection ({e}). "
            "This is usually a temporary rate limit — retry in a moment, "
            "or use --max-age to serve results from the local cache.",
            err=True,
        )
        return 1
    except SystemExit as e:
        typer.echo(str(e), err=True)
        return 1


def _run_cmd(func, **kwargs: object) -> None:
    raise typer.Exit(code=_invoke_cmd(func, **kwargs))


@app.command("today")
def today(
    keywords: list[str] | None = typer.Argument(
        None,
        help="Optional keyword filters. All keywords must match title/abstract/authors.",
    ),
    limit: int = typer.Option(20, "--limit", help="Max number of entries."),
    max_age: int | None = typer.Option(None, "--max-age", help="Cache max age in minutes (0 to force refresh). Defaults to config value."),
    category: list[str] | None = typer.Option(None, "--category", help="arXiv category, e.g. hep-th (overrides config, repeatable)."),
    abstract: int | None = typer.Option(None, "--abstract", help="Abstract lines to show (0=none, -1=full, N=first N lines). Defaults to config value."),
    highlight_keywords: int | None = typer.Option(None, "--highlight-keywords", help="Keyword matches to show per entry (0=glyph, -1=all, N=first N). Defaults to config value."),
) -> None:
    _run_cmd(cmd_today, limit=limit, keywords=keywords, max_age=max_age, category=category, abstract=abstract, highlight_keywords=highlight_keywords)


@app.command("search")
def search(
    query: list[str] | None = typer.Argument(None, help="Search terms."),
    limit: int = typer.Option(20, "--limit", help="Max number of entries."),
) -> None:
    _run_cmd(cmd_search, query=query, limit=limit)


@app.command("lastweek")
def lastweek(
    keywords: list[str] | None = typer.Argument(
        None,
        help="Optional keyword filters. All keywords must match title/abstract/authors.",
    ),
    limit: int = typer.Option(1000, "--limit", help="Max number of entries."),
    max_age: int | None = typer.Option(None, "--max-age", help="Cache max age in minutes (0 to force refresh). Defaults to config value."),
    category: list[str] | None = typer.Option(None, "--category", help="arXiv category, e.g. hep-th (overrides config, repeatable)."),
    abstract: int | None = typer.Option(None, "--abstract", help="Abstract lines to show (0=none, -1=full, N=first N lines). Defaults to config value."),
    highlight_keywords: int | None = typer.Option(None, "--highlight-keywords", help="Keyword matches to show per entry (0=glyph, -1=all, N=first N). Defaults to config value."),
) -> None:
    _run_cmd(cmd_lastweek, limit=limit, keywords=keywords, max_age=max_age, category=category, abstract=abstract, highlight_keywords=highlight_keywords)


@app.command("topvoted")
def topvoted(
    n: int = typer.Option(10, "--N", "--n", help="Number of entries to show."),
    repo: str | None = typer.Option(
        None,
        "--repo",
        help=f"GitHub repo owner/name. Only {DEFAULT_REPO} is accepted.",
    ),
    branch: str = typer.Option(
        os.getenv("CUHKVOTING_BRANCH", "main"),
        "--branch",
        help="Git branch to read/write.",
    ),
    abstract: int | None = typer.Option(
        None, "--abstract",
        help="Abstract lines to show (0=none, -1=full, N=first N). Defaults to config value.",
    ),
) -> None:
    _run_cmd(cmd_topvoted, N=n, repo=repo, branch=branch, abstract=abstract)


@app.command("record")
def record(
    repo: str | None = typer.Option(
        None,
        "--repo",
        help=f"GitHub repo owner/name. Only {DEFAULT_REPO} is accepted.",
    ),
    branch: str = typer.Option(
        os.getenv("CUHKVOTING_BRANCH", "main"),
        "--branch",
        help="Git branch to read/write.",
    ),
) -> None:
    _run_cmd(cmd_record, repo=repo, branch=branch)


@app.command("select")
def select(
    paper_id: str = typer.Argument(..., help="arXiv id/url."),
    repo: str | None = typer.Option(
        None,
        "--repo",
        help=f"GitHub repo owner/name. Only {DEFAULT_REPO} is accepted.",
    ),
    branch: str = typer.Option(
        os.getenv("CUHKVOTING_BRANCH", "main"),
        "--branch",
        help="Git branch to read/write.",
    ),
) -> None:
    _run_cmd(cmd_select, paper_id=paper_id, repo=repo, branch=branch)


@app.command("vote")
def vote_command(
    action_or_paper: list[str] | None = typer.Argument(
        None,
        help="One or more arXiv ids/urls OR action `remove <id>` (use `cuhkvoting select <id>`).",
    ),
    repo: str | None = typer.Option(
        None,
        "--repo",
        help=f"GitHub repo owner/name. Only {DEFAULT_REPO} is accepted.",
    ),
    branch: str = typer.Option(
        os.getenv("CUHKVOTING_BRANCH", "main"),
        "--branch",
        help="Git branch to read/write.",
    ),
) -> None:
    if not action_or_paper:
        _run_cmd(cmd_topvoted, N=10, repo=repo, branch=branch)
        return
    action = action_or_paper[0].strip().lower()
    if action == "remove":
        if len(action_or_paper) < 2:
            _run_cmd(cmd_topvoted, N=10, repo=repo, branch=branch)
            return
        if len(action_or_paper) > 2:
            raise typer.BadParameter("Usage: cuhkvoting vote remove <id>")
        resolved_remove = _resolve_paper_ids([action_or_paper[1]])
        _run_cmd(cmd_vote_remove, paper_id=resolved_remove[0][0], repo=repo, branch=branch)
        return
    if action == "select":
        if len(action_or_paper) < 2:
            raise typer.BadParameter("Usage: cuhkvoting select <id>")
        if len(action_or_paper) > 2:
            raise typer.BadParameter("Usage: cuhkvoting select <id>")
        _run_cmd(cmd_select, paper_id=action_or_paper[1], repo=repo, branch=branch)
        return
    resolved = _resolve_paper_ids(action_or_paper)
    cfg = _load_config()
    has_index = any(idx is not None for _, _, idx in resolved)
    if has_index and cfg.confirm_by_number:
        max_idx = max(idx for _, _, idx in resolved if idx is not None)
        w = len(str(max_idx))
        typer.echo("You are about to vote for:")
        for arxiv_id, title, idx in resolved:
            if idx is not None:
                typer.echo(f"  {idx:>{w}}. {arxiv_id}  {title or ''}")
            else:
                typer.echo(f"  {' ' * (w + 2)}{arxiv_id}  {title or ''}")
        typer.confirm("Proceed?", abort=True)
    token = _get_token()
    user = _resolve_user(token)
    display_name = cfg.display_name
    _warn_if_display_name_changed(display_name)
    repo_cfg = _resolve_repo_config(SimpleNamespace(repo=repo, branch=branch))

    # Resolve title/url for each paper from local cache, falling back to arXiv.
    papers_meta: list[dict] = []
    for arxiv_id, title, _ in resolved:
        if not title:
            cached = _lookup_local_cache(arxiv_id)
            if cached:
                title = cached.get("title", "")
            else:
                entry = _validate_arxiv_entry(arxiv_id)
                title = entry.get("title", "")
        papers_meta.append({
            "paper_id": arxiv_id,
            "title": title or "",
            "url": f"{ARXIV_ABS}{arxiv_id}",
        })

    try:
        if _has_github_ssh_access():
            # One clone → write all files → single commit/push
            _batch_vote_papers_ssh(repo_cfg, user, papers_meta, display_name)
        elif token:
            # One GraphQL read (per file) → parallel blob POSTs → single commit
            # Avoids the O(N) legacy-paper scan that cmd_vote triggers for new papers
            _batch_vote_papers_api(repo_cfg, token, user, papers_meta, display_name)
        else:
            raise SystemExit(f"Voting needs auth. Set CUHKVOTING_TOKEN/GITHUB_TOKEN or configure SSH key.\n\n{_ssh_setup_instructions()}")
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


@app.command("show")
def show_command(
    paper_ids: list[str] = typer.Argument(
        ..., help="arXiv IDs, list indices, or dates (YYYY-MM-DD / M-DD / A..B range).",
    ),
    category: list[str] | None = typer.Option(
        None, "--category", help="arXiv category filter (date mode only).", show_default=False,
    ),
    abstract: int | None = typer.Option(
        None, "--abstract", help="Abstract lines per entry: 0=none, -1=full, N=first N (date mode only).", show_default=False,
    ),
    limit: int = typer.Option(200, "--limit", help="Max entries to show (date mode only)."),
    highlight_keywords: int | None = typer.Option(
        None, "--highlight-keywords", help="Keyword match count to show (date mode only).", show_default=False,
    ),
) -> None:
    """Show paper details by arXiv ID/index, or list papers for a date/range."""
    date_spans: list[tuple[dt.date, dt.date]] = []
    keywords: list[str] = []
    for tok in paper_ids:
        span = _parse_date_token(tok)
        if span:
            date_spans.append(span)
        else:
            keywords.append(tok)

    if date_spans:
        _run_cmd(cmd_show_date, date_spans=date_spans, keywords=keywords,
                 categories=category, limit=limit, abstract=abstract,
                 highlight_keywords=highlight_keywords)
    else:
        _run_cmd(cmd_show, paper_ids=paper_ids)


@app.command("init-config")
def init_config(
    force: bool = typer.Option(False, "--force", help="Overwrite existing config file."),
) -> None:
    """Create a default config file at ~/.config/cuhkvoting/voting.toml."""
    if CONFIG_PATH.exists() and not force:
        typer.echo(f"Config already exists: {CONFIG_PATH}  (use --force to overwrite)")
        raise typer.Exit(code=1)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        '# arXiv categories for today/lastweek queries.\n'
        '# Supports wildcards, e.g. "astro-ph.*" matches all astro-ph subcategories.\n'
        'categories = ["gr-qc", "astro-ph.*"]\n'
        '\n'
        '[cache]\n'
        'today_max_age = 60      # minutes\n'
        'lastweek_max_age = 360  # minutes\n'
        '\n'
        '[display]\n'
        '# Number of abstract lines to show per entry.\n'
        '# 0 = none (default), -1 = full abstract, N = first N wrapped lines.\n'
        'abstract_lines = 0\n'
        'abstract_wrap = 80      # line wrap width in characters\n'
        '\n'
        '[vote]\n'
        '# Show a confirmation prompt when voting by list index (e.g. cuhkvoting vote 3).\n'
        'confirm_by_number = true\n'
        '# Human-readable name stored in the shared display_names.json table. GitHub username is used if empty.\n'
        'display_name = ""\n'
        '\n'
        '[highlights]\n'
        '# Authors to highlight. Format: "Surname, Firstname" (case-insensitive).\n'
        'authors = []\n'
        '# Keywords to highlight (regular expressions).\n'
        'keywords = []\n'
        '# Number of matched words to show after each title.\n'
        '# 0 = glyph only, -1 = all matches, N = first N distinct matches.\n'
        'keyword_count = -1\n'
        '# Glyph shown when keyword_count = 0.\n'
        'glyph = "★"\n',
        encoding="utf-8",
    )
    typer.echo(f"Config written to {CONFIG_PATH}")


admin_app = typer.Typer(name="admin", help="Admin-like maintenance commands (no admin auth required).")


@admin_app.command("trash")
def admin_trash(
    vote_id: str | None = typer.Argument(
        None,
        help="Vote record id (use arXiv id/url).",
    ),
    repo: str | None = typer.Option(
        None,
        "--repo",
        help=f"GitHub repo owner/name. Only {DEFAULT_REPO} is accepted.",
    ),
    branch: str = typer.Option(
        os.getenv("CUHKVOTING_BRANCH", "main"),
        "--branch",
        help="Git branch to read/write.",
    ),
) -> None:
    if not vote_id:
        _run_cmd(cmd_topvoted, N=10, repo=repo, branch=branch)
        return
    _run_cmd(cmd_admin_trash, vote_id=vote_id, repo=repo, branch=branch)


@admin_app.command("sanitize")
def admin_sanitize(
    repo: str | None = typer.Option(
        None, "--repo",
        help=f"GitHub repo owner/name. Only {DEFAULT_REPO} is accepted.",
    ),
    branch: str = typer.Option(
        os.getenv("CUHKVOTING_BRANCH", "main"),
        "--branch",
        help="Git branch to read/write.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing."),
) -> None:
    """Normalize whitespace in title/abstract and strip legacy display_name fields from vote entries."""
    args = SimpleNamespace(repo=repo, branch=branch)
    cfg = _resolve_repo_config(args)
    token = _get_token()
    user = _resolve_user(token)

    def _sanitize_paper(paper: dict) -> list[str]:
        """Apply all sanitizations in-place; return list of change descriptions (empty = no change)."""
        reasons: list[str] = []
        new_title    = " ".join(paper.get("title",    "").split())
        new_abstract = " ".join(paper.get("abstract", "").split())
        if new_title != paper.get("title"):
            paper["title"] = new_title
            reasons.append("whitespace in title")
        if new_abstract != paper.get("abstract"):
            paper["abstract"] = new_abstract
            reasons.append("whitespace in abstract")
        for v in paper.get("votes", []):
            if "display_name" in v:
                del v["display_name"]
                reasons.append(f"legacy display_name removed ({v.get('user', '?')})")
        return reasons

    typer.echo("Sanitizing: whitespace normalization, legacy display_name removal.")

    if _has_github_ssh_access():
        clone_dir = _with_repo_checkout(cfg)
        try:
            papers_dir = Path(clone_dir) / "papers"
            changed = 0
            for path in sorted(papers_dir.glob("*.json")) if papers_dir.exists() else []:
                if path.name == "journal_club_records.json":
                    continue
                try:
                    paper = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                reasons = _sanitize_paper(paper)
                if not reasons:
                    continue
                typer.echo(f"  {path.name}  ({'; '.join(reasons)})")
                if not dry_run:
                    path.write_text(json.dumps(paper, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                changed += 1
            if changed == 0:
                typer.echo("All records already clean.")
                return
            if dry_run:
                typer.echo(f"Would sanitize {changed} record(s).")
                return
            _ensure_commit_identity(clone_dir, user)
            _run_git(["add", "papers/"], cwd=clone_dir)
            _run_git(["commit", "-m", f"sanitize: {changed} paper record(s)"], cwd=clone_dir)
            _run_git(["push", "origin", f"HEAD:{cfg.branch}"], cwd=clone_dir)
            typer.echo(f"Sanitized {changed} record(s).")
        finally:
            shutil.rmtree(clone_dir, ignore_errors=True)
        return

    if not token:
        raise SystemExit(f"Sanitize needs auth. Set CUHKVOTING_TOKEN/GITHUB_TOKEN or configure SSH key.\n\n{_ssh_setup_instructions()}")

    # API path: fetch all blobs in parallel, then batch-commit all changes in one round-trip
    base_url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}"
    json_headers = {**_github_headers(token), "Content-Type": "application/json"}

    url = f"{base_url}/git/trees/{cfg.branch}?recursive=1"
    data = _http_json(url, headers=_github_headers(token))
    paper_items = [
        obj for obj in data.get("tree", [])
        if obj.get("type") == "blob"
        and obj.get("path", "").startswith("papers/")
        and obj.get("path", "").endswith(".json")
        and obj["path"] != "papers/journal_club_records.json"
    ]

    def _fetch_blob_content(item: dict) -> tuple[str, dict | None]:
        try:
            blob = _http_json(f"{base_url}/git/blobs/{item['sha']}", headers=_github_headers(token))
            content = base64.b64decode(blob["content"]).decode("utf-8")
            return item["path"], json.loads(content)
        except Exception:
            return item["path"], None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        fetched = list(ex.map(_fetch_blob_content, paper_items))

    changed = 0
    updates: list[tuple[str, dict]] = []
    for path, paper in fetched:
        if paper is None:
            continue
        reasons = _sanitize_paper(paper)
        if not reasons:
            continue
        typer.echo(f"  {path}  ({'; '.join(reasons)})")
        changed += 1
        if not dry_run:
            updates.append((path, paper))

    if changed == 0:
        typer.echo("All records already clean.")
        return
    if dry_run:
        typer.echo(f"Would sanitize {changed} record(s).")
        return

    def _post_sanitized_blob(path_paper: tuple[str, dict]) -> tuple[str, str]:
        path, paper = path_paper
        content = base64.b64encode(
            (json.dumps(paper, indent=2, sort_keys=True) + "\n").encode("utf-8")
        ).decode("ascii")
        resp = _http_json_request(
            f"{base_url}/git/blobs", "POST", {"content": content, "encoding": "base64"}, headers=json_headers
        )
        return path, resp["sha"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(updates))) as ex:
        blob_results = list(ex.map(_post_sanitized_blob, updates))

    tree_entries = [
        {"path": path, "mode": "100644", "type": "blob", "sha": sha}
        for path, sha in blob_results
    ]
    _git_batch_commit(
        base_url, json_headers, token, cfg.branch, tree_entries,
        f"sanitize: {changed} paper record(s)",
    )
    typer.echo(f"Sanitized {changed} record(s).")


app.add_typer(admin_app, name="admin")


def main() -> None:
    app()
