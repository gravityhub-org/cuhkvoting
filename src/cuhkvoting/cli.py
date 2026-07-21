from __future__ import annotations

import base64
import concurrent.futures
import http.client
import datetime as dt
import email.utils
import fnmatch
import importlib.metadata
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
from typing import NoReturn

import typer


try:
    _PKG_VERSION = importlib.metadata.version("cuhkvoting")
except importlib.metadata.PackageNotFoundError:
    _PKG_VERSION = "dev"
USER_AGENT = f"cuhkvoting/{_PKG_VERSION} (+https://github.com/gravityhub-org/cuhkvoting)"
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_ABS = "https://arxiv.org/abs/"
ARXIV_HTTP_TIMEOUT = 15
ARXIV_RETRY_DELAYS = (2, 5, 10)
ARXIV_QUERY_MAX_SECONDS = 60
ARXIV_FOUNDING_DATE = dt.date(1991, 8, 14)
INSPIRE_API = "https://inspirehep.net/api/literature"
INSPIRE_ARXIV_RECORD = "https://inspirehep.net/api/arxiv"
INSPIRE_HTTP_TIMEOUT = 15
INSPIRE_RETRY_DELAYS = (1, 2, 5)
INSPIRE_QUERY_MAX_SECONDS = 30
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
META_PATH = "meta.json"
META_SCHEMA_VERSION = 1
UPGRADE_COMMAND = "uv tool install --upgrade git+ssh://git@github.com/gravityhub-org/cuhkvoting.git"
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


class TitleUnresolved(Exception):
    """A paper's title could not be resolved because arXiv has no such id (typo guard).

    Distinct from a network failure: this means arXiv answered and the id is absent,
    so callers can skip just this paper. Network errors keep their native types
    (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead)
    and mean "arXiv unreachable" — voting must not block on those.
    """


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
    display_name_overrides: dict[str, str]
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
            dn_overrides = vote_cfg.get("display_names", {})
            dn_overrides = {str(k): str(v) for k, v in dn_overrides.items()} if isinstance(dn_overrides, dict) else {}
            return Config(
                categories=cats if isinstance(cats, list) and cats else DEFAULT_CATEGORIES,
                today_max_age=int(cache_cfg.get("today_max_age", DEFAULT_TODAY_MAX_AGE)),
                lastweek_max_age=int(cache_cfg.get("lastweek_max_age", DEFAULT_LASTWEEK_MAX_AGE)),
                abstract_lines=int(display_cfg.get("abstract_lines", DEFAULT_ABSTRACT_LINES)),
                abstract_wrap=int(display_cfg.get("abstract_wrap", DEFAULT_ABSTRACT_WRAP)),
                confirm_by_number=bool(vote_cfg.get("confirm_by_number", True)),
                display_name=str(vote_cfg.get("display_name", "")),
                display_name_overrides=dn_overrides,
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
        display_name_overrides={},
        highlight_authors=DEFAULT_HIGHLIGHT_AUTHORS,
        highlight_keywords=DEFAULT_HIGHLIGHT_KEYWORDS,
        highlight_keyword_count=DEFAULT_HIGHLIGHT_KEYWORD_COUNT,
        highlight_glyph=DEFAULT_HIGHLIGHT_GLYPH,
    )


def _http_text(url: str, headers: dict[str, str] | None = None, *, timeout: float = 30) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    # Force non-interactive git/ssh: a missing or locked credential should fail
    # fast with a readable error, never block on a prompt whose text we capture
    # (stderr is piped) and so would be invisible to the user.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new")
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        env=env,
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
        "- Samson Leong <samson.hwleong@gmail.com>\n"
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


def _get_user_from_ssh() -> str | None:
    """Return GitHub login parsed from the SSH auth banner, or None."""
    proc = subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", "git@github.com"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"Hi ([^!]+)!", text)
    return m.group(1) if m else None


def _github_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _gh_cli_token() -> str | None:
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (FileNotFoundError, OSError):
        pass
    return None


def _get_token() -> str | None:
    token = os.getenv("CUHKVOTING_TOKEN") or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        return token
    token = _gh_cli_token()
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
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
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


def _github_http_error_read(e: urllib.error.HTTPError) -> tuple[int, str]:
    try:
        body = e.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    return e.code, body


def _exit_github_api_http_from_body(token: str | None, code: int, raw: str) -> NoReturn:
    """Always raises SystemExit; never leaks GitHub JSON error bodies."""
    low = raw.lower()
    if code == 429 or (code == 403 and "rate limit" in low):
        if token:
            raise SystemExit("GitHub API rate limit — retry in a few minutes.") from None
        raise SystemExit(
            "GitHub needs credentials (anonymous API blocked).\n"
            "  export GITHUB_TOKEN=…   |   gh auth login   |   SSH key → ssh -T git@github.com"
        ) from None
    raise SystemExit(f"GitHub API HTTP {code}. Check GITHUB_TOKEN or repo access.") from None


def _load_papers_json_from_dir(papers_dir: Path) -> list[dict]:
    papers: list[dict] = []
    if not papers_dir.exists():
        return papers
    for path in sorted(papers_dir.glob("*.json")):
        try:
            papers.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return papers


def _list_papers_via_git_clone(cfg: RepoConfig) -> list[dict]:
    clone_dir = _with_repo_checkout(cfg)
    try:
        return _load_papers_json_from_dir(Path(clone_dir) / "papers")
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def _find_legacy_paper_via_git(cfg: RepoConfig, base_id: str) -> tuple[dict | None, str | None, str | None]:
    clone_dir = _with_repo_checkout(cfg)
    try:
        papers_dir = Path(clone_dir) / "papers"
        if not papers_dir.exists():
            return None, None, None
        for cand in papers_dir.glob("*.json"):
            try:
                paper = json.loads(cand.read_text(encoding="utf-8"))
            except Exception:
                continue
            if _strip_arxiv_version(str(paper.get("id", ""))) == base_id:
                return paper, None, str(cand.relative_to(clone_dir))
        return None, None, None
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def _load_paper_via_git_clone(cfg: RepoConfig, path: str) -> tuple[dict | None, str | None]:
    clone_dir = _with_repo_checkout(cfg)
    try:
        paper_file = Path(clone_dir) / path
        if not paper_file.exists():
            return None, None
        return json.loads(paper_file.read_text(encoding="utf-8")), None
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def _load_paper_via_api(cfg: RepoConfig, path: str, token: str | None) -> tuple[dict | None, str | None]:
    if not token and _has_github_ssh_access():
        return _load_paper_via_git_clone(cfg, path)
    url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}/contents/{path}?ref={cfg.branch}"
    try:
        data = _http_json(url, headers=_github_headers(token))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        code, body = _github_http_error_read(e)
        _exit_github_api_http_from_body(token, code, body)
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
    if not token and _has_github_ssh_access():
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


def _load_jc_records_and_display_names(
    cfg: RepoConfig, token: str | None
) -> tuple[list[dict], str | None, dict[str, str]]:
    """JC records (+sha) and the shared display-name table in one round-trip.

    Bundles the two reads `record`/`select` would otherwise do separately, so adding name
    resolution costs no extra request: the SSH path reads both files from one clone; the token
    path fetches both blobs in a single GraphQL query (the records `oid` doubles as the
    Contents-API sha used by later writes). Falls back to two plain reads on GraphQL failure.
    """
    if not token and _has_github_ssh_access():
        clone_dir = _with_repo_checkout(cfg)
        try:
            records: list[dict] = []
            rec_path = Path(clone_dir) / JC_RECORD_PATH
            if rec_path.exists():
                body = json.loads(rec_path.read_text(encoding="utf-8"))
                recs = body.get("records", []) if isinstance(body, dict) else []
                records = recs if isinstance(recs, list) else []
            dn_table: dict[str, str] = {}
            dn_path = Path(clone_dir) / DISPLAY_NAMES_PATH
            if dn_path.exists():
                parsed = json.loads(dn_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    dn_table = {str(k): str(v) for k, v in parsed.items()}
            meta_path = Path(clone_dir) / META_PATH
            _warn_if_client_outdated(
                _parse_meta_text(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            )
            return records, None, dn_table
        finally:
            shutil.rmtree(clone_dir, ignore_errors=True)
    if token:
        try:
            query = '{ repository(owner: "%s", name: "%s") { %s %s %s } }' % (
                cfg.owner,
                cfg.repo,
                f'rec: object(expression: "{cfg.branch}:{JC_RECORD_PATH}") {{ ... on Blob {{ text oid }} }}',
                f'dn: object(expression: "{cfg.branch}:{DISPLAY_NAMES_PATH}") {{ ... on Blob {{ text }} }}',
                f'meta: object(expression: "{cfg.branch}:{META_PATH}") {{ ... on Blob {{ text }} }}',
            )
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
            repo_node = (data.get("data") or {}).get("repository") or {}
            rec_node = repo_node.get("rec") or {}
            records = []
            rec_text = rec_node.get("text")
            if rec_text:
                body = json.loads(rec_text)
                recs = body.get("records", []) if isinstance(body, dict) else []
                records = recs if isinstance(recs, list) else []
            sha = rec_node.get("oid")
            dn_table = {}
            dn_text = (repo_node.get("dn") or {}).get("text")
            if dn_text:
                parsed = json.loads(dn_text)
                if isinstance(parsed, dict):
                    dn_table = {str(k): str(v) for k, v in parsed.items()}
            _warn_if_client_outdated(_parse_meta_text((repo_node.get("meta") or {}).get("text")))
            return records, sha, dn_table
        except (urllib.error.URLError, json.JSONDecodeError, RuntimeError, KeyError,
                TimeoutError, http.client.IncompleteRead):
            pass
    # Fallback: two plain reads (preserves today's behavior on GraphQL failure / no SSH).
    records, sha = _load_jc_records(cfg, token)
    return records, sha, _fetch_display_names(cfg, token)


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


def _save_meta(cfg: RepoConfig, token: str | None, user: str, body: dict, sha: str | None, message: str) -> None:
    """Write root meta.json, branching token (Contents API) vs SSH clone, like _save_jc_records."""
    if token:
        _save_json_via_api(cfg, META_PATH, body, sha, token, message)
        return
    if not _has_github_ssh_access():
        raise SystemExit(f"Writing meta needs auth. Set CUHKVOTING_TOKEN/GITHUB_TOKEN or configure SSH key.\n\n{_ssh_setup_instructions()}")
    clone_dir = _with_repo_checkout(cfg)
    try:
        p = Path(clone_dir) / META_PATH
        p.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _ensure_commit_identity(clone_dir, user)
        _run_git(["add", META_PATH], cwd=clone_dir)
        _run_git(["commit", "-m", message], cwd=clone_dir)
        _run_git(["push", "origin", f"HEAD:{cfg.branch}"], cwd=clone_dir)
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def _selected_arxiv_ids(cfg: RepoConfig, token: str | None) -> set[str]:
    """Canonical arXiv ids already recorded as journal-club selections."""
    records, _ = _load_jc_records(cfg, token)
    return {
        _strip_arxiv_version(str(r.get("arxiv_id", "")))
        for r in records
        if r.get("arxiv_id")
    }


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
    if not token and _has_github_ssh_access():
        return _list_papers_via_git_clone(cfg)
    url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}/git/trees/{cfg.branch}?recursive=1"
    try:
        data = _http_json(url, headers=_github_headers(token))
    except urllib.error.HTTPError as e:
        if e.code in (404, 409):
            return []
        code, body = _github_http_error_read(e)
        _exit_github_api_http_from_body(token, code, body)
    papers: list[dict] = []
    for obj in data.get("tree", []):
        path = obj.get("path", "")
        if obj.get("type") != "blob" or not path.startswith("papers/") or not path.endswith(".json"):
            continue
        paper, _sha = _load_paper_via_api(cfg, path, token)
        if paper:
            papers.append(paper)
    return papers


def _parse_retry_after(headers) -> int | None:
    """Return the Retry-After value in seconds, clamped to [1, 300]; None if absent/unparseable."""
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(1, min(300, int(raw)))
    except ValueError:
        pass
    try:
        target = email.utils.parsedate_to_datetime(raw)
        if target.tzinfo is None:
            target = target.replace(tzinfo=dt.timezone.utc)
        seconds = int((target - dt.datetime.now(dt.timezone.utc)).total_seconds())
        return max(1, min(300, seconds))
    except (TypeError, ValueError):
        return None


def _arxiv_retry_label(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if code == 503:
        return "arXiv overloaded"
    if code == 429:
        return "arXiv rate limit"
    if isinstance(exc, TimeoutError):
        return "arXiv timeout"
    if isinstance(exc, http.client.IncompleteRead):
        return "arXiv incomplete response"
    reason = getattr(exc, "reason", None) or str(exc)
    return f"arXiv connection error: {reason}"


def _arxiv_query(params: dict[str, str], *, delays: tuple[int, ...] = ARXIV_RETRY_DELAYS) -> list[dict[str, str]]:
    url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
    max_attempts = len(delays)
    deadline = time.monotonic() + ARXIV_QUERY_MAX_SECONDS
    _retry_label = "arXiv error"
    next_delay: int | None = None
    last_error: BaseException | None = None
    xml_str: str | None = None

    for attempt in range(max_attempts + 1):
        if attempt > 0:
            base_delay = next_delay if next_delay is not None else delays[attempt - 1]
            sleep_for = min(base_delay, max(0, int(deadline - time.monotonic())))
            if sleep_for <= 0:
                break
            for remaining in range(sleep_for, 0, -1):
                sys.stderr.write(
                    f"\r{_retry_label} (attempt {attempt}/{max_attempts}), retrying in {remaining}s… "
                )
                sys.stderr.flush()
                time.sleep(1)
            sys.stderr.write("\r" + " " * 70 + "\r")
            sys.stderr.flush()
            next_delay = None
        try:
            xml_str = _http_text(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=ARXIV_HTTP_TIMEOUT,
            )
            break
        except (ConnectionError, TimeoutError, urllib.error.URLError, http.client.IncompleteRead) as e:
            last_error = e
            _retry_label = _arxiv_retry_label(e)
            code = getattr(e, "code", None)
            if attempt >= max_attempts or time.monotonic() >= deadline:
                break
            if code is not None and code not in (429, 503):
                break
            if code in (429, 503):
                next_delay = _parse_retry_after(getattr(e, "headers", None))

    if xml_str is None:
        # Exhausted/non-retryable: re-raise the original network error (not SystemExit) so
        # callers' INSPIRE and stale-cache fallbacks (which catch the URLError family) trigger.
        raise last_error if last_error is not None else RuntimeError("arXiv query failed")

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


def _first_inspire_value(items: object, key: str) -> str:
    """First non-empty `key` string across a list of INSPIRE sub-records, whitespace-collapsed."""
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and isinstance(item.get(key), str) and item.get(key, "").strip():
                return " ".join(item[key].split())
    return ""


def _parse_inspire_metadata(metadata: object, *, arxiv_id: str | None = None) -> dict | None:
    """Parse one INSPIRE record's `metadata` into our entry shape, or None if unusable.

    arxiv_id: force the id (single-record lookup by arXiv id). When None, derive it from
    arxiv_eprints and return None if absent — list hits must stay arXiv-only.
    """
    if not isinstance(metadata, dict):
        return None
    eprints = metadata.get("arxiv_eprints", [])
    if arxiv_id is None:
        if isinstance(eprints, list):
            for ep in eprints:
                if isinstance(ep, dict) and isinstance(ep.get("value"), str) and ep.get("value", "").strip():
                    arxiv_id = _strip_arxiv_version(ep["value"])
                    break
        if not arxiv_id:
            return None
    authors: list[str] = []
    authors_raw = metadata.get("authors", [])
    if isinstance(authors_raw, list):
        for author in authors_raw:
            if isinstance(author, dict):
                name = author.get("full_name")
                if isinstance(name, str) and name.strip():
                    authors.append(" ".join(name.split()))
    cats: list[str] = []
    if isinstance(eprints, list) and eprints and isinstance(eprints[0], dict):
        raw_cats = eprints[0].get("categories", [])
        if isinstance(raw_cats, list):
            cats = [str(c) for c in raw_cats if str(c).strip()]
    earliest_date = metadata.get("earliest_date")
    if not isinstance(earliest_date, str):
        earliest_date = ""
    preprint_date = metadata.get("preprint_date")
    if not isinstance(preprint_date, str):
        preprint_date = ""
    return {
        "id": arxiv_id,
        "title": _first_inspire_value(metadata.get("titles"), "title"),
        "abstract": _first_inspire_value(metadata.get("abstracts"), "value"),
        "url": f"{ARXIV_ABS}{arxiv_id}",
        "authors": authors,
        "published": preprint_date or earliest_date,
        "primary_category": cats[0] if cats else "",
    }


def _inspire_query(query: str, limit: int) -> list[dict[str, str]]:
    params = {
        "q": query,
        "size": str(limit),
        "sort": "mostrecent",
        "fields": "titles,abstracts,authors,arxiv_eprints,control_number,earliest_date,preprint_date",
    }
    url = f"{INSPIRE_API}?{urllib.parse.urlencode(params)}"
    data = _http_json(url, headers={"User-Agent": USER_AGENT})
    hits = data.get("hits", {}).get("hits", [])
    entries: list[dict[str, str]] = []
    for hit in hits if isinstance(hits, list) else []:
        metadata = hit.get("metadata", {}) if isinstance(hit, dict) else {}
        entry = _parse_inspire_metadata(metadata)
        if entry is None or not entry["title"]:
            continue
        entries.append(entry)
    return entries


def _inspire_retry_label(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if code == 429:
        return "INSPIRE rate limit"
    if code in (502, 503, 504):
        return "INSPIRE unavailable"
    if isinstance(exc, TimeoutError):
        return "INSPIRE timeout"
    if isinstance(exc, http.client.IncompleteRead):
        return "INSPIRE incomplete response"
    reason = getattr(exc, "reason", None) or str(exc)
    return f"INSPIRE connection error: {reason}"


def _inspire_query_retry(query: str, limit: int) -> list[dict[str, str]]:
    delays = INSPIRE_RETRY_DELAYS
    max_attempts = len(delays)
    deadline = time.monotonic() + INSPIRE_QUERY_MAX_SECONDS
    _retry_label = "INSPIRE error"
    last_error: BaseException | None = None

    for attempt in range(max_attempts + 1):
        if attempt > 0:
            delay = min(delays[attempt - 1], max(0, int(deadline - time.monotonic())))
            if delay <= 0:
                break
            time.sleep(delay)
        try:
            # _inspire_query uses _http_json which has 30s timeout; avoid that.
            params = {
                "q": query,
                "size": str(limit),
                "sort": "mostrecent",
                "fields": "titles,abstracts,authors,arxiv_eprints,control_number,earliest_date,preprint_date",
            }
            url = f"{INSPIRE_API}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=INSPIRE_HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            hits = data.get("hits", {}).get("hits", [])
            parsed: list[dict[str, str]] = []
            for hit in hits if isinstance(hits, list) else []:
                metadata = hit.get("metadata", {}) if isinstance(hit, dict) else {}
                entry = _parse_inspire_metadata(metadata)
                if entry is None or not entry["title"]:
                    continue
                parsed.append(entry)
            return parsed
        except (ConnectionError, TimeoutError, urllib.error.URLError, http.client.IncompleteRead) as e:
            last_error = e
            _retry_label = _inspire_retry_label(e)
            code = getattr(e, "code", None)
            if attempt >= max_attempts or time.monotonic() >= deadline:
                break
            if code is not None and code not in (429, 502, 503, 504):
                break

    # Exhausted/non-retryable: re-raise the original network error (not SystemExit) so the
    # caller's stale-cache fallback (which catches the URLError family) can trigger.
    raise last_error if last_error is not None else RuntimeError("INSPIRE query failed")


def _inspire_get_by_arxiv_id(paper_id: str) -> dict | None:
    clean = _strip_arxiv_version(paper_id)
    url = f"{INSPIRE_ARXIV_RECORD}/{urllib.parse.quote(clean)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=INSPIRE_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    metadata = data.get("metadata", {})
    return _parse_inspire_metadata(metadata, arxiv_id=clean)


def _notify_inspire_fallback() -> None:
    """One-line notice that arXiv is unreachable and INSPIRE-HEP is being used instead."""
    typer.echo(
        typer.style("Note: arXiv unavailable — falling back to INSPIRE-HEP.", fg=typer.colors.YELLOW),
        err=True,
    )


def _fetch_entries(categories: list[str], start: dt.date, end: dt.date, limit: int) -> list[dict]:
    # arXiv is primary; INSPIRE is the fallback when arXiv is unreachable. The arXiv attempt
    # is single and timer-less (delays=()), so the failover to INSPIRE is invisible (no retry
    # countdown). If INSPIRE also fails it re-raises → caller's stale-cache fallback handles it.
    start_dt = dt.datetime.combine(start, dt.time.min)
    end_dt = dt.datetime.combine(end, dt.time.max).replace(second=0, microsecond=0)
    try:
        return _arxiv_query(
            {
                "search_query": (
                    f"{_build_cat_query(categories)} AND "
                    f"submittedDate:[{start_dt.strftime('%Y%m%d%H%M')} TO {end_dt.strftime('%Y%m%d%H%M')}]"
                ),
                "start": "0",
                "max_results": str(limit),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
            delays=(),
        )
    except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead):
        # INSPIRE stores arXiv categories in arxiv_eprints.categories and dates in earliest_date
        # (day-granular). Fallback only — INSPIRE indexing lags arXiv for the newest papers.
        _notify_inspire_fallback()
        cats_q = "(" + " or ".join(f"arxiv_eprints.categories:{c}" for c in categories) + ")"
        q = f"{cats_q} and earliest_date:[{start.isoformat()} to {end.isoformat()}]"
        return _inspire_query_retry(q, limit)


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
    return {"user": user, "voted_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")}


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
    if not token and _has_github_ssh_access():
        return _find_legacy_paper_via_git(cfg, base_id)
    url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}/git/trees/{cfg.branch}?recursive=1"
    try:
        data = _http_json(url, headers=_github_headers(token))
    except urllib.error.HTTPError as e:
        if e.code in (404, 409):
            return None, None, None
        code, body = _github_http_error_read(e)
        if not token and _has_github_ssh_access() and (
            code == 429 or (code == 403 and "rate limit" in body.lower())
        ):
            return _find_legacy_paper_via_git(cfg, base_id)
        _exit_github_api_http_from_body(token, code, body)
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
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _format_age(td: dt.timedelta) -> str:
    total = int(td.total_seconds())
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        h, m = divmod(total // 60, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem_h = divmod(total // 3600, 24)
    return f"{d}d {rem_h}h" if rem_h else f"{d}d"


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


def _vote_user_set(votes: list[dict]) -> frozenset[str]:
    return frozenset(
        str(v.get("user", "")).strip().lower()
        for v in votes
        if str(v.get("user", "")).strip()
    )


def _diversify_topvoted_rows(rows: list[dict]) -> list[dict]:
    """Order rows by vote count, spreading distinct voters within ties before date."""
    if len(rows) <= 1:
        return rows
    by_votes: dict[int, list[dict]] = {}
    for row in rows:
        by_votes.setdefault(row["votes"], []).append(row)
    ordered: list[dict] = []
    for vote_count in sorted(by_votes.keys(), reverse=True):
        remaining = list(by_votes[vote_count])
        seen_voters: set[str] = set()
        while remaining:
            best = min(
                remaining,
                key=lambda r: (
                    len(r["voter_users"] & seen_voters),
                    -r["latest_vote_ts"],
                    r["id"],
                ),
            )
            remaining.remove(best)
            ordered.append(best)
            seen_voters |= best["voter_users"]
    return ordered


def _prune_expired_votes(paper: dict) -> int:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=VOTE_EXPIRY_DAYS)
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
        user = _get_user_from_ssh()
    if not user:
        raise SystemExit(
            "Could not identify GitHub user. "
            "Set CUHKVOTING_USER, CUHKVOTING_TOKEN/GITHUB_TOKEN, or configure a GitHub SSH key."
        )
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
    # arXiv is primary (single, timer-less attempt). INSPIRE is the fallback only when arXiv
    # is unreachable — keeps the invisible-failover behaviour and avoids INSPIRE's indexing lag
    # for brand-new ids. arXiv reachable-but-empty stays a hard "not found" (typo guard).
    try:
        entries = _arxiv_query({"search_query": f"id:{paper_id}", "start": "0", "max_results": "1"}, delays=())
    except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead):
        _notify_inspire_fallback()
        try:
            entry = _inspire_get_by_arxiv_id(paper_id)
        except Exception:
            entry = None
        if entry:
            return entry
        raise  # re-raise the arXiv network error → friendly top-level handler
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


def _save_cache(key: str, categories: list[str], entries: list[dict], extra: dict | None = None) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "categories": sorted(categories),
        "entries": entries,
    }
    if extra:
        data.update(extra)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def _cache_is_fresh(data: dict | None, categories: list[str], max_age_seconds: int) -> bool:
    if not data or not data.get("entries"):
        return False
    fetched_at = _parse_utc(data.get("fetched_at", ""))
    if fetched_at is None or (dt.datetime.now(dt.timezone.utc) - fetched_at).total_seconds() > max_age_seconds:
        return False
    return set(data.get("categories", [])) == set(categories)


def _seed_today_cache(entries: list[dict], categories: list[str]) -> None:
    """Overwrite the `today` cache with the latest announced batch in `entries`."""
    latest = max((e.get("published", "")[:10] for e in entries if e.get("published")), default="")
    batch = [e for e in entries if e.get("published", "").startswith(latest)] if latest else []
    _save_cache("today", categories, batch)


def _seed_lastweek_cache(entries: list[dict], categories: list[str]) -> None:
    """Overwrite the `lastweek` cache with the last-7-days subset of `entries`."""
    cutoff = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=7)).isoformat()
    _save_cache("lastweek", categories, [e for e in entries if e.get("published", "")[:10] >= cutoff])


def _resolve_last_n_window(days: int, categories: list[str], max_age_seconds: int) -> list[dict]:
    """Return the shared `last-n` window (a superset covering >= `days`), refetching and
    overwriting `last-n.json` when it's stale, category-mismatched, or too narrow."""
    data = _load_cache_data("last-n")
    if _cache_is_fresh(data, categories, max_age_seconds) and data.get("window_days", 0) >= days:
        return list(data.get("entries", []))
    try:
        entries = _fetch_lastdays_entries(days, categories)
    except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead) as exc:
        if data and data.get("entries"):
            typer.echo(
                typer.style(
                    f"Warning: arXiv unreachable ({exc}); serving cached 'last-n' data.",
                    fg=typer.colors.YELLOW,
                ),
                err=True,
            )
            return list(data.get("entries", []))
        raise
    _save_cache("last-n", categories, entries, {"window_days": days})
    return entries


def _lookup_local_cache(paper_id: str) -> dict | None:
    for key in ("today", "lastweek"):
        data = _load_cache_data(key)
        if not data:
            continue
        for entry in data.get("entries", []):
            if _strip_arxiv_version(str(entry.get("id", ""))) == paper_id:
                return entry
    return None


def _resolve_vote_metadata(arxiv_id: str, title: str | None = None) -> dict:
    """Return vote metadata with a non-empty title when the paper exists on arXiv/INSPIRE."""
    clean_id = _strip_arxiv_version(arxiv_id)
    resolved_title = (title or "").strip()
    cached = _lookup_local_cache(clean_id)
    if not resolved_title and cached:
        resolved_title = (cached.get("title") or "").strip()
    if not resolved_title:
        try:
            entry = _validate_arxiv_entry(clean_id)
        except SystemExit:
            # arXiv answered but has no such id — a typo, not a network outage.
            # Network failures re-raise their native types from _validate_arxiv_entry.
            raise TitleUnresolved(clean_id) from None
        resolved_title = (entry.get("title") or "").strip()
        if not resolved_title:
            raise TitleUnresolved(clean_id)
        return {
            "paper_id": clean_id,
            "title": resolved_title,
            "url": entry.get("url", f"{ARXIV_ABS}{clean_id}"),
            "abstract": entry.get("abstract", ""),
        }
    return {
        "paper_id": clean_id,
        "title": resolved_title,
        "url": (cached or {}).get("url", f"{ARXIV_ABS}{clean_id}"),
        "abstract": (cached or {}).get("abstract", ""),
    }


def _resolve_batch_metadata(
    resolved: list[tuple[str, str | None, int | None]],
) -> tuple[list[dict], list[str]]:
    """Resolve vote metadata for a batch, isolating per-paper failures.

    Returns (papers_meta, skipped). A paper is skipped only when arXiv is reachable
    and reports the id does not exist (a typo). When arXiv is unreachable, voting must
    not block: the paper is kept with whatever title we already have (possibly empty),
    for `admin sanitize` to backfill later.
    """
    papers_meta: list[dict] = []
    skipped: list[str] = []
    for arxiv_id, title, _ in resolved:
        try:
            papers_meta.append(_resolve_vote_metadata(arxiv_id, title))
        except TitleUnresolved:
            typer.echo(
                typer.style(f"{arxiv_id} not found on arXiv (typo?); skipping.", fg=typer.colors.YELLOW),
                err=True,
            )
            skipped.append(arxiv_id)
        except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead):
            clean_id = _strip_arxiv_version(arxiv_id)
            typer.echo(
                typer.style(
                    f"arXiv unreachable; voting {clean_id} with known title — "
                    "run 'admin sanitize' to backfill.",
                    fg=typer.colors.YELLOW,
                ),
                err=True,
            )
            papers_meta.append({
                "paper_id": clean_id,
                "title": (title or ""),
                "url": f"{ARXIV_ABS}{clean_id}",
            })
    return papers_meta, skipped


def _apply_paper_metadata(paper: dict, meta: dict) -> None:
    """Backfill missing title/url/abstract on an existing paper record."""
    title = (meta.get("title") or "").strip()
    if title and not (paper.get("title") or "").strip():
        paper["title"] = title
    url = meta.get("url")
    if url and not paper.get("url"):
        paper["url"] = url
    abstract = meta.get("abstract", "")
    if abstract and not paper.get("abstract"):
        paper["abstract"] = abstract


def _backfill_paper_metadata(paper: dict) -> list[str]:
    """Fetch and apply missing title/url/abstract. Returns change descriptions."""
    reasons: list[str] = []
    paper_id = _strip_arxiv_version(str(paper.get("id", "")))
    if not paper_id:
        return reasons
    needs_title = not (paper.get("title") or "").strip()
    needs_abstract = not (paper.get("abstract") or "").strip()
    needs_url = not (paper.get("url") or "").strip()
    if not (needs_title or needs_abstract or needs_url):
        return reasons
    try:
        meta = _resolve_vote_metadata(paper_id, None if needs_title else paper.get("title"))
    except TitleUnresolved:
        if needs_title:
            reasons.append(f"title backfill failed ({paper_id})")
        return reasons
    except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead):
        reasons.append(f"metadata backfill skipped, arXiv unreachable ({paper_id})")
        return reasons
    before = (paper.get("title"), paper.get("abstract"), paper.get("url"))
    _apply_paper_metadata(paper, meta)
    if needs_title and paper.get("title") != before[0]:
        reasons.append("title backfilled")
    if needs_abstract and paper.get("abstract") != before[1]:
        reasons.append("abstract backfilled")
    if needs_url and paper.get("url") != before[2]:
        reasons.append("url backfilled")
    return reasons


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
        is_stale = fetched_at is None or (dt.datetime.now(dt.timezone.utc) - fetched_at).total_seconds() > max_age_seconds
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

    try:
        entries = fetch_fn(categories)
    except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead) as exc:
        if data is not None and data.get("entries"):
            age = _format_age(dt.datetime.now(dt.timezone.utc) - fetched_at) if fetched_at else "unknown age"
            typer.echo(
                typer.style(
                    f"Warning: arXiv unreachable ({exc}); serving cached '{key}' data ({age} old).",
                    fg=typer.colors.YELLOW,
                ),
                err=True,
            )
            return list(data.get("entries", []))
        raise
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
    # arXiv doesn't announce on weekends; a strict 24-hour window would return nothing
    # on Monday morning. Query a 7-day window and keep only the most recent announcement
    # date (published field). Sourced via _fetch_entries (arXiv primary, INSPIRE fallback).
    end_day = dt.datetime.now(dt.timezone.utc).date()
    start_day = end_day - dt.timedelta(days=7)
    all_entries = _fetch_entries(categories, start_day, end_day, limit=1000)
    latest_date = max(
        (e.get("published", "")[:10] for e in all_entries if e.get("published")),
        default="",
    )
    if not latest_date:
        return all_entries
    return [e for e in all_entries if e.get("published", "").startswith(latest_date)]


def _fetch_lastdays_entries(days: int, categories: list[str]) -> list[dict]:
    end_day = dt.datetime.now(dt.timezone.utc).date()
    start_day = end_day - dt.timedelta(days=days)
    return _fetch_entries(categories, start_day, end_day, limit=1000)


def _fetch_daterange_entries(start: dt.date, end: dt.date, categories: list[str]) -> list[dict]:
    return _fetch_entries(categories, start, end, limit=500)


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


# --- Client-version high-water-mark (root meta.json) --------------------------
# A passive, backward-compatible "your client is out of date" notice. The records
# repo carries a root meta.json holding the highest client version seen in the
# wild; clients read it only where a fetch is already happening (piggybacking a
# GraphQL query or an existing clone), warn when behind, and raise the value when
# ahead. Every helper below is total: it never raises, so a missing/garbled file
# or a network hiccup can never turn a vote into a failure. None means "no usable
# information" everywhere — the single fail-open value.

_RELEASE_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def _parse_release_version(value: object) -> tuple[int, int, int] | None:
    """Strict ``X.Y.Z`` -> (major, minor, patch); None for anything else.

    Deliberately stricter than PEP 440: ``packaging`` is unavailable, and a bare
    triple is exactly the set of versions this project releases. Any suffix
    (``rc``/``.dev``/``+local``), the ``"dev"`` not-installed fallback, and
    malformed input all yield None, which silences both warning and publishing.
    """
    if not isinstance(value, str):
        return None
    m = _RELEASE_RE.match(value.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _install_provenance() -> str | None:
    """"index"/"vcs"/"url" when this install's version is obtainable by others; else None.

    PEP 610: pip/uv write ``direct_url.json`` for direct installs only. A
    ``dir_info`` key marks a local-directory install (``pip install -e .`` or
    ``pip install .``), where the version in pyproject is a plan for a future
    release rather than a fact — such clients must never raise the shared
    high-water-mark. This project ships from git (README), so ``vcs_info`` is the
    normal publishing case; absent means a package index.
    """
    try:
        raw = importlib.metadata.distribution("cuhkvoting").read_text("direct_url.json")
    except Exception:
        return None
    if raw is None:
        return "index"
    try:
        info = json.loads(raw)
    except Exception:
        return None
    if not isinstance(info, dict) or "dir_info" in info:
        return None
    return "vcs" if "vcs_info" in info else "url"


def _parse_meta_text(text: str | None) -> dict | None:
    """Parsed meta.json. ``{}`` = absent (safe to seed). None = present-but-unusable.

    The None state is deliberate: seeding over a file we failed to parse would
    destroy whatever a future client wrote. Only ``admin set-version`` may
    overwrite that state.
    """
    if text is None:
        return {}
    try:
        doc = json.loads(text)
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def _client_outdated_message(meta_doc: dict | None) -> str | None:
    """Note text when a newer client version is recorded, else None. Never raises."""
    local = _parse_release_version(_PKG_VERSION)
    if local is None:
        return None
    client = meta_doc.get("client") if isinstance(meta_doc, dict) else None
    recorded = _parse_release_version(client.get("latest_version")) if isinstance(client, dict) else None
    if recorded is None or recorded <= local:
        return None
    # Render from the parsed ints, never the raw string: meta.json is world-writable
    # and echoing its bytes to a TTY would allow terminal escape injection.
    return (
        f"Note: cuhkvoting {recorded[0]}.{recorded[1]}.{recorded[2]} is available "
        f"(you have {_PKG_VERSION}).\n"
        f"      Upgrade: {UPGRADE_COMMAND}"
    )


def _warn_if_client_outdated(meta_doc: dict | None) -> None:
    """Passive note when a newer client version is recorded. Never raises/blocks."""
    msg = _client_outdated_message(meta_doc)
    if msg is not None:
        typer.echo(typer.style(msg, fg=typer.colors.YELLOW), err=True)


def _stamp_client_node(node: dict, version: str, user: str, source: str) -> None:
    """Set the client high-water-mark fields in place, preserving any unknown sub-keys.

    Single source of truth for the ``client`` block's schema, shared by the vote-path
    ratchet and the ``admin set-version`` repair command.
    """
    node["latest_version"] = version
    node["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    node["updated_by"] = user
    node["source"] = source


def _bump_meta_client_version(meta_doc: dict | None, user: str) -> dict | None:
    """Mutated copy of ``meta_doc`` raising the client high-water-mark, or None = do not write.

    Returns None (no write) unless the local version is a clean release, strictly
    higher than the recorded one, and this install is one others can obtain (see
    ``_install_provenance``). Mutates rather than replaces: keys this function does
    not own survive a round-trip, so meta.json can host unrelated namespaces later.
    """
    if not isinstance(meta_doc, dict):
        return None
    schema = meta_doc.get("schema", META_SCHEMA_VERSION)
    if not isinstance(schema, int) or schema > META_SCHEMA_VERSION:
        return None
    local = _parse_release_version(_PKG_VERSION)
    if local is None:
        return None
    source = _install_provenance()
    if source is None:
        return None
    client = meta_doc.get("client")
    recorded = _parse_release_version(client.get("latest_version")) if isinstance(client, dict) else None
    if recorded is not None and local <= recorded:
        return None

    new_doc = dict(meta_doc)  # shallow: unknown top-level keys preserved by reference
    node = dict(client) if isinstance(client, dict) else {}  # unknown client sub-keys preserved
    _stamp_client_node(node, _PKG_VERSION.strip(), user, source)
    new_doc["client"] = node
    new_doc["schema"] = META_SCHEMA_VERSION
    return new_doc


def _sanitize_for_terminal(text: str) -> str:
    """Drop control characters (incl. ANSI escapes) from untrusted text before echoing.

    meta.json is world-writable, so its free-text fields must never reach a TTY raw.
    isprintable() is already False for ESC and every other control character.
    """
    return "".join(ch for ch in text if ch == "\t" or ch.isprintable())


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


@dataclass
class ListData:
    """A browse pipeline's result: the list to display, decoupled from printing."""
    entries: list[dict]           # keyword-filtered and limit-truncated
    notes: list[str]              # advisory notes; the CLI styles them yellow
    header: str | None = None     # header line printed above a non-empty list
    empty_msg: str | None = None  # set iff entries is empty


def _today_list(args: SimpleNamespace, cfg: Config) -> ListData:
    categories = _parse_categories(getattr(args, "category", None), cfg.categories)
    max_age_minutes = getattr(args, "max_age", None)
    max_age_seconds = (int(max_age_minutes) if max_age_minutes is not None else cfg.today_max_age) * 60
    entries = _resolve_cache("today", categories, max_age_seconds, _fetch_today_entries)
    if not entries:
        return ListData([], [], empty_msg="No papers found for today (UTC).")
    batch_date = max(
        (e.get("published", "")[:10] for e in entries if e.get("published")),
        default="",
    )
    today_utc = dt.datetime.now(dt.timezone.utc).date().isoformat()
    notes = []
    if batch_date and batch_date != today_utc:
        notes.append(f"Note: no papers announced today (UTC); showing last available batch ({batch_date}).")
    entries = _filter_entries(entries, getattr(args, "keywords", None))
    if not entries:
        return ListData([], notes, empty_msg="No papers matched keyword filter.")
    return ListData(entries[: int(args.limit)], notes)


def cmd_today(args: SimpleNamespace) -> int:
    cfg = _load_config()
    data = _today_list(args, cfg)
    for note in data.notes:
        print(typer.style(note, fg=typer.colors.YELLOW))
    if data.empty_msg:
        print(data.empty_msg)
        return 0
    _save_last_list(data.entries)
    abstract_lines = getattr(args, "abstract", None)
    if abstract_lines is None:
        abstract_lines = cfg.abstract_lines
    highlight_kw_count = getattr(args, "highlight_keywords", None)
    if highlight_kw_count is None:
        highlight_kw_count = cfg.highlight_keyword_count
    _print_entry_list(data.entries, cfg, abstract_lines, highlight_kw_count)
    return 0


def _search_list(args: SimpleNamespace) -> ListData:
    query_parts = [str(q).strip() for q in (args.query or []) if str(q).strip()]
    if not query_parts:
        raise SystemExit("Search query cannot be empty.")
    query = _build_inspire_title_query(query_parts)
    requested_limit = int(args.limit)
    fetch_limit = min(max(requested_limit * 5, 100), 500)
    entries = _inspire_query(query, fetch_limit)
    entries = _filter_entries(entries, query_parts)
    if not entries:
        return ListData([], [], empty_msg="No matches.")
    return ListData(entries[:requested_limit], [])


def cmd_search(args: SimpleNamespace) -> int:
    data = _search_list(args)
    if data.empty_msg:
        print(data.empty_msg)
        return 0
    _save_last_list(data.entries)
    for idx, p in enumerate(data.entries, 1):
        pid = str(p.get("id", ""))
        print(f"{idx:>2}. {_format_clickable_id(pid)}  {p['title']}")
    return 0


def _lastweek_list(args: SimpleNamespace, cfg: Config) -> ListData:
    categories = _parse_categories(getattr(args, "category", None), cfg.categories)
    max_age_minutes = getattr(args, "max_age", None)
    max_age_seconds = (int(max_age_minutes) if max_age_minutes is not None else cfg.lastweek_max_age) * 60
    entries = _resolve_cache(
        "lastweek", categories, max_age_seconds,
        lambda cats: _fetch_lastdays_entries(7, cats),
    )
    _seed_today_cache(entries, categories)
    entries = _filter_entries(entries, getattr(args, "keywords", None))
    if not entries:
        return ListData([], [], empty_msg="No entries matched in last week (UTC).")
    return ListData(entries[: int(args.limit)], [])


def cmd_lastweek(args: SimpleNamespace) -> int:
    cfg = _load_config()
    data = _lastweek_list(args, cfg)
    if data.empty_msg:
        print(data.empty_msg)
        return 0
    _save_last_list(data.entries)
    abstract_lines = getattr(args, "abstract", None)
    if abstract_lines is None:
        abstract_lines = cfg.abstract_lines
    highlight_kw_count = getattr(args, "highlight_keywords", None)
    if highlight_kw_count is None:
        highlight_kw_count = cfg.highlight_keyword_count
    _print_entry_list(data.entries, cfg, abstract_lines, highlight_kw_count)
    return 0


def _lastdays_list(args: SimpleNamespace, cfg: Config) -> ListData:
    """List for `last <#>` with days not in {1, 7} (those delegate to today/lastweek)."""
    days = int(args.days)
    categories = _parse_categories(getattr(args, "category", None), cfg.categories)
    max_age_minutes = getattr(args, "max_age", None)
    max_age_seconds = (int(max_age_minutes) if max_age_minutes is not None else cfg.lastweek_max_age) * 60

    # A superset covering >= `days`: a fresh lastweek (for 2..6), else the shared last-n cache.
    full: list[dict] | None = None
    if days <= 6:
        lw = _load_cache_data("lastweek")
        if _cache_is_fresh(lw, categories, max_age_seconds):
            full = list(lw.get("entries", []))
    if full is None:
        full = _resolve_last_n_window(days, categories, max_age_seconds)

    # En-passant cache warming (unconditional), from the full superset.
    _seed_today_cache(full, categories)            # n >= 2
    if days > 7:
        _seed_lastweek_cache(full, categories)     # n > 7 also warms lastweek

    cutoff = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=days)).isoformat()
    entries = [e for e in full if e.get("published", "")[:10] >= cutoff]
    entries = _filter_entries(entries, getattr(args, "keywords", None))
    if not entries:
        return ListData([], [], empty_msg=f"No entries matched in the last {days} days (UTC).")
    displayed = entries[: int(args.limit)]
    return ListData(
        displayed, [],
        header=f"Papers from the last {days} days (since {cutoff}, UTC) — {len(displayed)} entries:",
    )


def cmd_last(args: SimpleNamespace) -> int:
    days = int(args.days)
    if days == 1:
        return cmd_today(args)        # last 1 ≡ today
    if days == 7:
        return cmd_lastweek(args)     # last 7 ≡ lastweek

    cfg = _load_config()
    data = _lastdays_list(args, cfg)
    if data.empty_msg:
        print(data.empty_msg)
        return 0
    _save_last_list(data.entries)
    print(data.header)
    abstract_lines = getattr(args, "abstract", None)
    if abstract_lines is None:
        abstract_lines = cfg.abstract_lines
    highlight_kw_count = getattr(args, "highlight_keywords", None)
    if highlight_kw_count is None:
        highlight_kw_count = cfg.highlight_keyword_count
    _print_entry_list(data.entries, cfg, abstract_lines, highlight_kw_count)
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
                "voter_users": _vote_user_set(votes),
                "latest_vote_ts": _latest_vote_timestamp(votes),
            }
        )
    return _diversify_topvoted_rows(rows)


def _topvoted_list(args: SimpleNamespace, cfg: Config) -> ListData:
    repo_cfg = _resolve_repo_config(args)
    token = _get_token()
    papers = _list_papers_via_api(repo_cfg, token)

    dn_table = {**_fetch_display_names(repo_cfg, token), **cfg.display_name_overrides}
    rows = _topvoted_rows_from_papers(papers, dn_table)
    n = getattr(args, "N", None)
    if n is not None:  # None = no cutoff (interactive mode)
        rows = rows[: int(n)]
    if not rows:
        return ListData([], [], empty_msg="No voted papers yet.")
    return ListData(rows, [])


def cmd_topvoted(args: SimpleNamespace) -> int:
    cfg = _load_config()
    data = _topvoted_list(args, cfg)
    if data.empty_msg:
        print(data.empty_msg)
        return 0
    topn = data.entries
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


def _show_date_list(args: SimpleNamespace, cfg: Config) -> ListData:
    date_spans: list[tuple[dt.date, dt.date]] = args.date_spans
    start = min(s for s, _ in date_spans)
    end   = max(e for _, e in date_spans)

    today = dt.date.today()
    if end > today:
        raise SystemExit("Date is in the future.")
    if start < ARXIV_FOUNDING_DATE:
        raise SystemExit("arXiv was founded on 1991-08-14.")

    categories = _parse_categories(getattr(args, "categories", None), cfg.categories)
    cache_key = f"date-{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
    all_entries = _resolve_cache(
        cache_key, categories, 999_999_999,
        lambda cats: _fetch_daterange_entries(start, end, cats),
    )

    entries = [e for e in all_entries if _entry_in_date_spans(e, date_spans)]
    entries = _filter_entries(entries, getattr(args, "keywords", None) or [])

    if not entries:
        return ListData([], [], empty_msg="No papers found for the requested date(s).")

    displayed = entries[: int(getattr(args, "limit", 200))]

    def _span_label(s: dt.date, e: dt.date) -> str:
        return s.isoformat() if s == e else f"{s.isoformat()}..{e.isoformat()}"
    header_label = ", ".join(_span_label(s, e) for s, e in date_spans)
    return ListData(displayed, [], header=f"Papers for {header_label} ({len(displayed)} entries):")


def cmd_show_date(args: SimpleNamespace) -> int:
    """Show papers for a specific date or date range, numbered for index-based vote."""
    cfg = _load_config()
    data = _show_date_list(args, cfg)
    if data.empty_msg:
        print(data.empty_msg)
        return 0
    _save_last_list(data.entries)
    print(data.header)
    abstract_lines = getattr(args, "abstract", None)
    if abstract_lines is None:
        abstract_lines = cfg.abstract_lines
    highlight_kw_count = getattr(args, "highlight_keywords", None)
    if highlight_kw_count is None:
        highlight_kw_count = cfg.highlight_keyword_count
    _print_entry_list(data.entries, cfg, abstract_lines, highlight_kw_count)
    return 0


@dataclass
class VoteResult:
    """Outcome of a batch vote."""
    voted: list[str]           # every processed paper_id, including already-voted
    new: list[str]             # only the paper_ids whose vote is genuinely new
    outdated_msg: str | None   # client out-of-date notice; None when current


def _batch_vote_papers_ssh(
    cfg: RepoConfig,
    user: str,
    papers: list[dict],
    display_name: str = "",
) -> VoteResult:
    """Vote for multiple papers in a single clone/commit/push cycle.

    Each dict in `papers` must have: paper_id, title, url.
    """
    if not papers:
        return VoteResult([], [], None)
    clone_dir = _with_repo_checkout(cfg)
    voted: list[str] = []
    new_votes: list[str] = []
    outdated_msg: str | None = None
    try:
        papers_dir = Path(clone_dir) / "papers"
        papers_dir.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        for p in papers:
            paper_id = _strip_arxiv_version(p["paper_id"])
            title = p.get("title", "")
            url = p.get("url", f"{ARXIV_ABS}{paper_id}")
            paper_file = papers_dir / f"{_safe_filename(paper_id)}.json"
            # No individual API GET — files are read directly from the local checkout,
            # preserving any existing votes from other users without extra round-trips.
            abstract = p.get("abstract", "")
            if paper_file.exists():
                try:
                    paper = json.loads(paper_file.read_text(encoding="utf-8"))
                except Exception:
                    paper = {"id": paper_id, "title": title, "abstract": abstract, "url": url, "votes": []}
            else:
                paper = {"id": paper_id, "title": title, "abstract": abstract, "url": url, "votes": []}
            _apply_paper_metadata(paper, p)
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

        # Client-version high-water-mark, read for free from the checkout. Warn
        # (behind) or self-update (ahead) — never both. The write only rides the
        # commit that new votes / display-name changes are already making; it never
        # forces one on its own (the gate stays `new_votes or dn_changed`).
        meta_file = Path(clone_dir) / META_PATH
        meta_doc = _parse_meta_text(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
        outdated_msg = _client_outdated_message(meta_doc)
        _warn_if_client_outdated(meta_doc)

        if new_votes or dn_changed:
            new_meta = _bump_meta_client_version(meta_doc, user)
            meta_changed = new_meta is not None
            if meta_changed:
                meta_file.write_text(json.dumps(new_meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            _ensure_commit_identity(clone_dir, user)
            if new_votes:
                _run_git(["add", "papers/"], cwd=clone_dir)
            if dn_changed:
                _run_git(["add", DISPLAY_NAMES_PATH], cwd=clone_dir)
            if meta_changed:
                _run_git(["add", META_PATH], cwd=clone_dir)
            if new_votes:
                ids_str = ", ".join(new_votes[:3]) + ("…" if len(new_votes) > 3 else "")
                msg = f"vote: {user} -> [{ids_str}] ({len(new_votes)} papers)"
            else:
                msg = f"display-name: update {user}"
            _run_git(["commit", "-m", msg], cwd=clone_dir)
            _run_git(["push", "origin", f"HEAD:{cfg.branch}"], cwd=clone_dir)
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)
    return VoteResult(voted, new_votes, outdated_msg)


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
) -> VoteResult:
    """Vote for multiple papers in a single Git commit via the GitHub Git Data API.

    Each dict in `papers` must have: paper_id, title, url.
    Reduces API calls from 2N sequential to N parallel blob POSTs + 5 overhead calls.
    """
    if not papers:
        return VoteResult([], [], None)

    base_url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}"
    json_headers = {**_github_headers(token), "Content-Type": "application/json"}
    ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")

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
    aliases.append(
        f'meta: object(expression: "{cfg.branch}:{META_PATH}") {{ ... on Blob {{ text }} }}'
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
                paper = {"id": paper_id, "title": title, "abstract": p.get("abstract", ""), "url": url, "votes": []}
        else:
            paper = {"id": paper_id, "title": title, "abstract": p.get("abstract", ""), "url": url, "votes": []}

        _apply_paper_metadata(paper, p)
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

    # Client-version high-water-mark, read for free from the same query. On this
    # invocation we either warn (behind) or self-update (ahead) — never both.
    meta_doc = _parse_meta_text((repo_node.get("meta") or {}).get("text"))
    outdated_msg = _client_outdated_message(meta_doc)
    _warn_if_client_outdated(meta_doc)

    if not updates:
        return VoteResult(voted, [], outdated_msg)

    # Ride the existing commit only — appended after the empty-updates guard so a
    # bump can never manufacture a standalone commit.
    new_meta = _bump_meta_client_version(meta_doc, user)
    if new_meta is not None:
        updates.append((META_PATH, "__meta__", new_meta))

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

    new_ids = [pid for _, pid, _ in updates if pid not in ("__dn__", "__meta__")]
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
    return VoteResult(voted, new_ids, outdated_msg)


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
    meta = {"paper_id": paper_id, "title": title, "url": url}
    if not (title or "").strip():
        try:
            meta = _resolve_vote_metadata(paper_id)
        except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead):
            # arXiv unreachable — vote with the (empty) title we have; sanitize backfills.
            pass
    if paper is None:
        paper = {
            "id": paper_id,
            "title": meta["title"],
            "abstract": meta.get("abstract", ""),
            "url": meta["url"],
            "votes": [],
        }
    else:
        _apply_paper_metadata(paper, meta)
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
        try:
            meta = _resolve_vote_metadata(paper_id)
        except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead):
            # arXiv unreachable — don't block the vote; sanitize can backfill later.
            clean_id = _strip_arxiv_version(paper_id)
            meta = {"paper_id": clean_id, "title": "", "url": f"{ARXIV_ABS}{clean_id}", "abstract": ""}
        paper = {
            "id": meta["paper_id"],
            "title": meta["title"],
            "abstract": meta.get("abstract", ""),
            "url": meta["url"],
            "votes": [],
        }
    else:
        # Existing local record: backfill is best-effort. A typo is impossible here
        # (the id matched a file), and an outage must not block the vote.
        try:
            meta = _resolve_vote_metadata(paper_id, paper.get("title"))
        except (TitleUnresolved, urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead):
            meta = None
        if meta:
            _apply_paper_metadata(paper, meta)

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
    app_cfg = _load_config()
    token = _get_token()
    user = _resolve_user(token)
    paper_id = _normalize_paper_id(args.paper_id)
    canonical_id = _strip_arxiv_version(paper_id)

    # The journal-club records are the source of truth for "already selected".
    # Checking them first also avoids the _load_vote_paper clone-fallback hang
    # when the vote file was already deleted by a prior select.
    records, record_sha, dn_table = _load_jc_records_and_display_names(cfg, token)
    name_table = {**dn_table, **app_cfg.display_name_overrides}
    existing = next(
        (r for r in records if _strip_arxiv_version(str(r.get("arxiv_id", ""))) == canonical_id),
        None,
    )
    if existing is not None:
        typer.echo(
            typer.style(
                f"Already selected: {canonical_id} ({existing.get('week', '?')}) "
                f"by {_resolve_display_name(str(existing.get('selected_by', '?')), name_table)} on {existing.get('selected_at', '?')}. "
                f"Use 'cuhkvoting select remove {canonical_id}' to undo.",
                fg=typer.colors.YELLOW,
            ),
            err=True,
        )
        return 0

    paper, sha, save_path = _load_vote_paper(cfg, token, paper_id)

    # Resolve a title without forcing an arXiv round-trip: cache > records > arXiv.
    # arXiv is the last resort; if even it can't confirm the id, the paper has never
    # been validated anywhere and the command is likely corrupt, so let it abort.
    cached = _lookup_local_cache(paper_id)
    if cached and cached.get("title"):
        title = cached["title"]
    elif paper and paper.get("title"):
        title = paper["title"]
    else:
        title = _validate_arxiv_entry(paper_id).get("title", "")

    if paper:
        _prune_expired_votes(paper)
    vote_count = len(paper.get("votes", [])) if paper else 0
    now = dt.datetime.now(dt.timezone.utc)
    year, week, _ = now.isocalendar()
    week_tag = f"{year}-W{week:02d}"
    selected_at = now.isoformat().replace("+00:00", "Z")

    records.append(
        {
            "week": week_tag,
            "arxiv_id": canonical_id,
            "title": title,
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

    print(f"Selected for presentation: {canonical_id} ({week_tag}) by {_resolve_display_name(user, name_table)}")
    return 0


def cmd_unselect(args: SimpleNamespace) -> int:
    cfg = _resolve_repo_config(args)
    token = _get_token()
    user = _resolve_user(token)
    canonical_id = _strip_arxiv_version(_normalize_paper_id(args.paper_id))

    records, record_sha = _load_jc_records(cfg, token)
    kept = [r for r in records if _strip_arxiv_version(str(r.get("arxiv_id", ""))) != canonical_id]
    removed = len(records) - len(kept)
    if removed == 0:
        typer.echo(
            typer.style(
                f"Not selected: {canonical_id} is not in the journal-club records.",
                fg=typer.colors.YELLOW,
            ),
            err=True,
        )
        return 0
    _save_jc_records(cfg, token, user, kept, record_sha, f"record-unselect: {user} -> {canonical_id}")
    noun = "entry" if removed == 1 else "entries"
    print(f"Unselected {canonical_id}: removed {removed} record {noun}.")
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


def cmd_admin_meta(args: SimpleNamespace) -> int:
    """Show the recorded client-version high-water-mark and its provenance."""
    cfg = _resolve_repo_config(args)
    token = _get_token()
    data, _sha = _load_json_via_api(cfg, META_PATH, token)
    client = data.get("client") if isinstance(data, dict) else None
    if not isinstance(client, dict) or not client.get("latest_version"):
        print("No client version recorded in meta.json yet.")
        return 0
    raw_version = str(client.get("latest_version", ""))
    version = _parse_release_version(raw_version)
    # Show a bad value (sanitized) rather than hiding it — this is the tool an admin
    # uses to find and fix a poisoned entry.
    version_str = (
        f"{version[0]}.{version[1]}.{version[2]}" if version
        else f"{_sanitize_for_terminal(raw_version)} (unparseable)"
    )
    # Render provenance defensively: meta.json is world-writable, so strip control
    # characters from free-text fields before echoing them to a terminal.
    by = _sanitize_for_terminal(str(client.get("updated_by", "?")))
    at = _sanitize_for_terminal(str(client.get("updated_at", "?")))
    source = _sanitize_for_terminal(str(client.get("source", "?")))
    print(f"Recorded client version: {version_str}")
    print(f"  updated by: {by}")
    print(f"  updated at: {at}")
    print(f"  source:     {source}")
    return 0


def cmd_admin_set_version(args: SimpleNamespace) -> int:
    """Unconditionally set or clear the client-version high-water-mark (repair/reset)."""
    cfg = _resolve_repo_config(args)
    token = _get_token()
    user = _resolve_user(token)
    clear = bool(getattr(args, "clear", False))
    new_version = getattr(args, "version", None)

    if not clear:
        if not new_version:
            raise SystemExit("Provide a version (X.Y.Z) or use --clear.")
        if _parse_release_version(new_version) is None:
            raise SystemExit(f"Not a valid release version: '{new_version}'. Expected X.Y.Z.")

    data, sha = _load_json_via_api(cfg, META_PATH, token)
    doc = dict(data) if isinstance(data, dict) else {}
    doc.setdefault("schema", META_SCHEMA_VERSION)  # seed a fresh file; never downgrade a future envelope

    if clear:
        if "client" not in doc:
            print("meta.json has no client version to clear.")
            return 0
        del doc["client"]
        message = f"meta: clear client version ({user})"
    else:
        node = dict(doc.get("client")) if isinstance(doc.get("client"), dict) else {}
        _stamp_client_node(node, new_version.strip(), user, "admin")
        doc["client"] = node
        message = f"meta: set client version {new_version.strip()} ({user})"

    if getattr(args, "dry_run", False):
        print("[dry-run] would write meta.json:")
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0

    _save_meta(cfg, token, user, doc, sha, message)
    print("Cleared client version in meta.json." if clear else f"Set client version to {new_version.strip()} in meta.json.")
    return 0


def cmd_record(args: SimpleNamespace) -> int:
    cfg = _resolve_repo_config(args)
    app_cfg = _load_config()
    token = _get_token()
    records, _sha, dn_table = _load_jc_records_and_display_names(cfg, token)
    if not records:
        print("No journal club records yet.")
        return 0
    name_table = {**dn_table, **app_cfg.display_name_overrides}
    rows = sorted(records, key=lambda r: str(r.get("selected_at", "")), reverse=True)
    for i, r in enumerate(rows, 1):
        week = str(r.get("week", "?"))
        arxiv_id = str(r.get("arxiv_id", "?"))
        title = str(r.get("title", "(no title)"))
        hist = int(r.get("historical_vote", 0))
        selected_by = _resolve_display_name(str(r.get("selected_by", "?")), name_table)
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
    except (TimeoutError, http.client.IncompleteRead) as e:
        typer.echo(
            f"arXiv request failed ({type(e).__name__}: {e}). "
            "This is usually a temporary issue — retry in a moment, "
            "or use --max-age to serve results from the local cache.",
            err=True,
        )
        return 1
    except TitleUnresolved as e:
        typer.echo(
            typer.style(f"{e} not found on arXiv (typo?).", fg=typer.colors.YELLOW),
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


@app.command("last")
def last(
    days: int = typer.Argument(..., help="Number of days back to list (e.g. 3)."),
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
    """List papers submitted in the last <#> days (UTC). `last 1` ≡ `today`, `last 7` ≡ `lastweek`."""
    if days < 1:
        raise typer.BadParameter("<#> must be a positive integer.")
    _run_cmd(cmd_last, days=days, limit=limit, keywords=keywords, max_age=max_age, category=category, abstract=abstract, highlight_keywords=highlight_keywords)


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
    action_or_paper: list[str] = typer.Argument(
        ..., help="arXiv id/url, or `remove <id>` to undo a selection."
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
    if action_or_paper[0].strip().lower() == "remove":
        if len(action_or_paper) != 2:
            raise typer.BadParameter("Usage: cuhkvoting select remove <id>")
        _run_cmd(cmd_unselect, paper_id=action_or_paper[1], repo=repo, branch=branch)
        return
    if len(action_or_paper) != 1:
        raise typer.BadParameter("Usage: cuhkvoting select <id>  (or: cuhkvoting select remove <id>)")
    _run_cmd(cmd_select, paper_id=action_or_paper[0], repo=repo, branch=branch)


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
    token = _get_token()
    repo_cfg = _resolve_repo_config(SimpleNamespace(repo=repo, branch=branch))

    # Drop papers already selected for a past journal club — they should not come back.
    selected = _selected_arxiv_ids(repo_cfg, token)
    if selected:
        kept = []
        for arxiv_id, title, idx in resolved:
            if _strip_arxiv_version(arxiv_id) in selected:
                typer.echo(
                    typer.style(
                        f"Skipping {arxiv_id}: already selected for a past journal club.",
                        fg=typer.colors.YELLOW,
                    ),
                    err=True,
                )
            else:
                kept.append((arxiv_id, title, idx))
        resolved = kept
    if not resolved:
        typer.echo("Nothing to vote for (all given papers were already selected).", err=True)
        raise typer.Exit(code=0)

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
    user = _resolve_user(token)
    display_name = cfg.display_name
    _warn_if_display_name_changed(display_name)

    # Resolve per paper: a typo (arXiv reachable, id absent) skips just that paper;
    # an arXiv outage keeps it with the known title. Neither aborts the batch.
    papers_meta, skipped = _resolve_batch_metadata(resolved)
    if not papers_meta:
        typer.echo("Nothing to vote for (no resolvable arXiv ids).", err=True)
        raise typer.Exit(code=1)

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
    # Nonzero when some ids were skipped as typos, so the mistake is visible in $?.
    raise typer.Exit(code=1 if skipped else 0)


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


@app.command("interactive")
def interactive_command(
    tokens: list[str] | None = typer.Argument(
        None,
        help="today (default) | lastweek | last <#> | topvoted | search <kw...> | show <date> | <date> | <keywords>",
    ),
) -> None:
    """Vim-style full-screen voting session (needs the 'interactive' extra)."""
    from cuhkvoting import interactive as tui
    if not tui.PROMPT_TOOLKIT_OK:
        typer.echo(
            "Interactive mode needs prompt_toolkit. Install with:\n"
            '  pip install "cuhkvoting[interactive]"',
            err=True,
        )
        raise typer.Exit(code=1)
    raise typer.Exit(code=tui.run(tokens or []))


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
        '# Local-only overrides for how OTHER people\'s names show on YOUR screen (never uploaded).\n'
        '# Keys are GitHub usernames; values are what to display instead.\n'
        '# [vote.display_names]\n'
        '# octocat = "Mona Lisa"\n'
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
        'glyph = "★"\n'
        '\n'
        '[interactive]\n'
        '# Interactive-mode (cuhkvoting interactive) settings; see docs/interactive.md.\n'
        'theme = "default"       # default, onedark, gruvbox, catppuccin-mocha, solarized-dark, nord\n'
        'key_hints = true        # show key hints on the idle input line\n'
        'follow = false          # start with zi follow mode (selected abstract always open)\n',
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
    """Normalize records, backfill missing titles, and strip legacy display_name fields."""
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
        reasons.extend(_backfill_paper_metadata(paper))
        return reasons

    def _sanitize_jc_records(body: dict) -> list[str]:
        """Normalize and de-duplicate journal-club records in-place; return change descriptions."""
        reasons: list[str] = []
        records = body.get("records")
        if not isinstance(records, list):
            return reasons

        # 1. Normalize fields in place: title whitespace, canonical (version-stripped) arxiv_id.
        for r in records:
            if not isinstance(r, dict):
                continue
            new_title = " ".join(str(r.get("title", "")).split())
            if new_title != r.get("title"):
                r["title"] = new_title
                reasons.append(f"whitespace in title ({r.get('arxiv_id', '?')})")
            raw_id = str(r.get("arxiv_id", ""))
            canon = _strip_arxiv_version(raw_id)
            if canon != r.get("arxiv_id"):
                r["arxiv_id"] = canon
                reasons.append(f"arxiv_id normalized ({raw_id} -> {canon})")
            if not (r.get("title") or "").strip() and canon:
                try:
                    meta = _resolve_vote_metadata(canon)
                except TitleUnresolved:
                    reasons.append(f"title backfill failed ({canon})")
                except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead):
                    reasons.append(f"title backfill skipped, arXiv unreachable ({canon})")
                else:
                    new_title = meta.get("title", "")
                    if new_title:
                        r["title"] = new_title
                        reasons.append(f"title backfilled ({canon})")

        # 2. De-duplicate by canonical arxiv_id, keeping the earliest selection
        #    (earliest selected_at; missing dates sort last). Preserve first-seen order.
        best: dict[str, dict] = {}
        order: list[str] = []
        for r in records:
            if not isinstance(r, dict):
                continue
            canon = str(r.get("arxiv_id", ""))
            if not canon:
                continue
            if canon not in best:
                best[canon] = r
                order.append(canon)
            elif (str(r.get("selected_at", "")) or "~") < (str(best[canon].get("selected_at", "")) or "~"):
                best[canon] = r
        deduped = [best[c] for c in order]
        removed = len(records) - len(deduped)
        if removed:
            reasons.append(f"{removed} duplicate selection(s) removed")
        if deduped != records:
            body["records"] = deduped
        return reasons

    typer.echo("Sanitizing: whitespace normalization, missing title backfill, "
               "legacy display_name removal, journal-club record de-duplication.")

    if _has_github_ssh_access():
        clone_dir = _with_repo_checkout(cfg)
        try:
            papers_dir = Path(clone_dir) / "papers"
            changed = 0
            for path in sorted(papers_dir.glob("*.json")) if papers_dir.exists() else []:
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if path.name == "journal_club_records.json":
                    reasons = _sanitize_jc_records(doc)
                else:
                    reasons = _sanitize_paper(doc)
                if not reasons:
                    continue
                typer.echo(f"  {path.name}  ({'; '.join(reasons)})")
                if not dry_run:
                    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
    for path, doc in fetched:
        if doc is None:
            continue
        if path == JC_RECORD_PATH:
            reasons = _sanitize_jc_records(doc)
        else:
            reasons = _sanitize_paper(doc)
        if not reasons:
            continue
        typer.echo(f"  {path}  ({'; '.join(reasons)})")
        changed += 1
        if not dry_run:
            updates.append((path, doc))

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


@admin_app.command("meta")
def admin_meta(
    repo: str | None = typer.Option(
        None, "--repo",
        help=f"GitHub repo owner/name. Only {DEFAULT_REPO} is accepted.",
    ),
    branch: str = typer.Option(
        os.getenv("CUHKVOTING_BRANCH", "main"),
        "--branch",
        help="Git branch to read/write.",
    ),
) -> None:
    """Show the recorded client-version high-water-mark and its provenance."""
    _run_cmd(cmd_admin_meta, repo=repo, branch=branch)


@admin_app.command("set-version")
def admin_set_version(
    version: str | None = typer.Argument(
        None,
        help="Release version X.Y.Z to record. Omit with --clear.",
    ),
    clear: bool = typer.Option(False, "--clear", help="Remove the client version entry."),
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
    """Unconditionally set or clear the client-version high-water-mark (repair/reset)."""
    _run_cmd(cmd_admin_set_version, version=version, clear=clear, repo=repo, branch=branch, dry_run=dry_run)


app.add_typer(admin_app, name="admin")


def main() -> None:
    app()
