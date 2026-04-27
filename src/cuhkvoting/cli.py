from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import typer


ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_ABS = "https://arxiv.org/abs/"


@dataclass
class RepoConfig:
    owner: str
    repo: str
    branch: str

    @property
    def ssh_clone_url(self) -> str:
        return f"git@github.com:{self.owner}/{self.repo}.git"


def _http_text(url: str, headers: dict[str, str] | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _http_json(url: str, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_put_json(url: str, payload: dict, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers or {},
        method="PUT",
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


def _derive_repo_from_git() -> tuple[str, str, str] | None:
    try:
        out = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            parsed = _parse_repo_url(out)
            if parsed:
                return parsed[0], parsed[1], out
    except Exception:
        return None
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
        raise SystemExit(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def _resolve_repo_config(args: SimpleNamespace) -> RepoConfig:
    repo_arg = args.repo or os.getenv("CUHKVOTING_REPO")
    owner: str | None = None
    repo: str | None = None
    if repo_arg:
        if "/" not in repo_arg:
            raise SystemExit("Repo must look like owner/name, e.g. gravityhub-org/cuhkvoting")
        owner, repo = repo_arg.split("/", 1)
    else:
        parsed = _derive_repo_from_git()
        if parsed:
            owner, repo, _remote_url = parsed
    if not owner or not repo:
        raise SystemExit(
            "Could not determine GitHub repo. Set CUHKVOTING_REPO=owner/name or pass --repo owner/name."
        )
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
        "User-Agent": "cuhkvoting/0.1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_token() -> str | None:
    return os.getenv("CUHKVOTING_TOKEN") or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")


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


def _save_paper_via_api(
    cfg: RepoConfig, path: str, paper: dict, sha: str | None, token: str, user: str, paper_vote_id: str
) -> None:
    url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}/contents/{path}"
    payload = {
        "message": f"vote: {user} -> {paper_vote_id}",
        "branch": cfg.branch,
        "content": base64.b64encode((json.dumps(paper, indent=2, sort_keys=True) + "\n").encode("utf-8")).decode(
            "ascii"
        ),
    }
    if sha:
        payload["sha"] = sha
    _http_put_json(url, payload, headers=_github_headers(token))


def _list_papers_via_api(cfg: RepoConfig, token: str | None) -> list[dict]:
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
    xml_str = _http_text(url, headers={"User-Agent": "cuhkvoting/0.1"})
    root = ET.fromstring(xml_str)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries: list[dict[str, str]] = []
    for ent in root.findall("atom:entry", ns):
        entry_id = (ent.findtext("atom:id", "", ns) or "").strip()
        title = " ".join((ent.findtext("atom:title", "", ns) or "").split())
        summary = " ".join((ent.findtext("atom:summary", "", ns) or "").split())
        arxiv_id = entry_id.rsplit("/", 1)[-1]
        entries.append(
            {"id": arxiv_id, "title": title, "abstract": summary, "url": f"{ARXIV_ABS}{arxiv_id}"}
        )
    return entries


def _normalize_paper_id(raw_id: str) -> str:
    raw_id = raw_id.strip()
    if raw_id.startswith("http://") or raw_id.startswith("https://"):
        raw_id = raw_id.rstrip("/").rsplit("/", 1)[-1]
    return raw_id.replace("arXiv:", "")


def _safe_filename(paper_id: str) -> str:
    return paper_id.replace("/", "__")


def _with_repo_checkout(cfg: RepoConfig) -> tuple[str, str]:
    tmpdir = tempfile.mkdtemp(prefix="cuhkvoting-")
    try:
        _run_git(["clone", "--depth", "1", "--branch", cfg.branch, cfg.ssh_clone_url, tmpdir])
        return tmpdir, tmpdir
    except SystemExit:
        shutil.rmtree(tmpdir, ignore_errors=True)
        tmpdir = tempfile.mkdtemp(prefix="cuhkvoting-")
        _run_git(["clone", "--depth", "1", cfg.ssh_clone_url, tmpdir])
        try:
            _run_git(["checkout", cfg.branch], cwd=tmpdir)
        except SystemExit:
            _run_git(["checkout", "-b", cfg.branch], cwd=tmpdir)
    return tmpdir, tmpdir


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


def cmd_today(args: SimpleNamespace) -> int:
    now = dt.datetime.utcnow()
    start_dt = now - dt.timedelta(days=1)
    start = start_dt.strftime("%Y%m%d%H%M")
    end = now.strftime("%Y%m%d%H%M")
    params = {
        "search_query": f"submittedDate:[{start} TO {end}]",
        "start": "0",
        "max_results": str(args.limit),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    entries = _arxiv_query(params)
    if not entries:
        # arXiv may have no new submissions in current UTC window.
        entries = _arxiv_query(
            {
                "search_query": "all:the",
                "start": "0",
                "max_results": str(args.limit),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        if not entries:
            print("No papers found for today (UTC).")
            return 0
        print("No new UTC-day submissions. Showing most recent arXiv entries:")
    for idx, p in enumerate(entries, 1):
        print(f"{idx:>2}. {p['id']}  {p['title']}")
    return 0


def cmd_search(args: SimpleNamespace) -> int:
    query = args.query.strip()
    params = {
        "search_query": f"all:{query}",
        "start": "0",
        "max_results": str(args.limit),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    entries = _arxiv_query(params)
    if not entries:
        print("No matches.")
        return 0
    for idx, p in enumerate(entries, 1):
        print(f"{idx:>2}. {p['id']}  {p['title']}")
    return 0


def cmd_lastweek(args: SimpleNamespace) -> int:
    end_day = dt.datetime.utcnow().date()
    start_day = end_day - dt.timedelta(days=7)
    start = start_day.strftime("%Y%m%d")
    end = end_day.strftime("%Y%m%d")
    params = {
        "search_query": f"(cat:gr-qc OR cat:astro-ph.*) AND submittedDate:[{start}0000 TO {end}2359]",
        "start": "0",
        "max_results": str(args.limit),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    entries = _arxiv_query(params)
    if not entries:
        print("No gr-qc / astro-ph entries in the last week (UTC).")
        return 0
    for idx, p in enumerate(entries, 1):
        print(f"{idx:>2}. {p['id']}  {p['title']}")
    return 0


def cmd_topvoted(args: SimpleNamespace) -> int:
    cfg = _resolve_repo_config(args)
    token = _get_token()
    papers = _list_papers_via_api(cfg, token)
    if not papers and _has_github_ssh_access():
        clone_dir, cleanup_dir = _with_repo_checkout(cfg)
        try:
            papers_dir = Path(clone_dir) / "papers"
            for path in sorted(papers_dir.glob("*.json")) if papers_dir.exists() else []:
                try:
                    papers.append(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
        finally:
            shutil.rmtree(cleanup_dir, ignore_errors=True)

    rows = []
    for paper in papers:
        rows.append(
            {
                "id": paper.get("id", "(unknown)"),
                "title": paper.get("title", "(no title)"),
                "votes": len(paper.get("votes", [])),
            }
        )
    rows.sort(key=lambda p: (-p["votes"], p["id"]))
    topn = rows[: args.N]
    if not topn:
        print("No voted papers yet.")
        return 0
    for idx, p in enumerate(topn, 1):
        print(f"{idx:>2}. [{p['votes']:>3} votes] {p['id']}  {p['title']}")
    return 0


def cmd_vote(args: SimpleNamespace) -> int:
    cfg = _resolve_repo_config(args)
    token = _get_token()
    user = os.getenv("CUHKVOTING_USER")
    if not user:
        user = os.getenv("GITHUB_USER")
    if not user:
        user = _get_user_from_token(token)
    if not user:
        # Use git config user.name as default identity if explicit user is not set.
        try:
            user = _run_git(["config", "--global", "user.name"]).strip()
        except SystemExit:
            user = ""
    if not user:
        raise SystemExit("Could not identify user. Set CUHKVOTING_USER or configure git user.name.")

    paper_id = _normalize_paper_id(args.paper_id)
    path = f"papers/{_safe_filename(paper_id)}.json"

    paper, sha = _load_paper_via_api(cfg, path, token)
    if paper is None and _has_github_ssh_access():
        clone_dir, cleanup_dir = _with_repo_checkout(cfg)
        try:
            paper_file = Path(clone_dir) / path
            if paper_file.exists():
                paper = json.loads(paper_file.read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(cleanup_dir, ignore_errors=True)

    if paper is None:
        entries = _arxiv_query(
            {
                "search_query": f"id:{paper_id}",
                "start": "0",
                "max_results": "1",
            }
        )
        if not entries:
            raise SystemExit(f"Could not find arXiv entry for id '{paper_id}'.")
        entry = entries[0]
        paper = {
            "id": entry["id"],
            "title": entry["title"],
            "abstract": entry["abstract"],
            "url": entry["url"],
            "votes": [],
        }
        sha = None

    votes = paper.setdefault("votes", [])
    if any(v.get("user") == user for v in votes):
        raise SystemExit(f"User '{user}' already voted for {paper.get('id', paper_id)}.")
    paper_vote_id = paper.get("id", paper_id)
    votes.append({"user": user, "voted_at": dt.datetime.utcnow().isoformat() + "Z"})

    if token:
        _save_paper_via_api(cfg, path, paper, sha, token, user, paper_vote_id)
        print(f"Vote recorded: {user} -> {paper_vote_id}")
        return 0

    if not _has_github_ssh_access():
        raise SystemExit(
            "Voting needs auth. Set CUHKVOTING_TOKEN/GITHUB_TOKEN or configure GitHub SSH key."
        )

    clone_dir, cleanup_dir = _with_repo_checkout(cfg)
    try:
        paper_file = Path(clone_dir) / path
        paper_file.parent.mkdir(parents=True, exist_ok=True)
        paper_file.write_text(json.dumps(paper, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _ensure_commit_identity(clone_dir, user)
        _run_git(["add", str(Path(path))], cwd=clone_dir)
        _run_git(["commit", "-m", f"vote: {user} -> {paper_vote_id}"], cwd=clone_dir)
        _run_git(["push", "origin", f"HEAD:{cfg.branch}"], cwd=clone_dir)
        print(f"Vote recorded: {user} -> {paper_vote_id}")
        return 0
    finally:
        shutil.rmtree(cleanup_dir, ignore_errors=True)


app = typer.Typer(
    name="cuhkvoting",
    help="Minimal arXiv voting CLI backed by GitHub.",
    add_completion=True,
)


def _run_cmd(func, **kwargs: object) -> None:
    args = SimpleNamespace(**kwargs)
    try:
        code = func(args)
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="ignore")
        typer.echo(f"HTTP {e.code}: {msg[:300]}", err=True)
        raise typer.Exit(code=1) from e
    except urllib.error.URLError as e:
        typer.echo(f"Network error: {e.reason}", err=True)
        raise typer.Exit(code=1) from e
    except SystemExit as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    raise typer.Exit(code=code)


@app.command("today")
def today(
    limit: int = typer.Option(20, "--limit", help="Max number of entries."),
) -> None:
    _run_cmd(cmd_today, limit=limit)


@app.command("search")
def search(
    query: str = typer.Argument(..., help="Search terms."),
    limit: int = typer.Option(20, "--limit", help="Max number of entries."),
) -> None:
    _run_cmd(cmd_search, query=query, limit=limit)


@app.command("lastweek")
def lastweek(
    limit: int = typer.Option(100, "--limit", help="Max number of entries."),
) -> None:
    _run_cmd(cmd_lastweek, limit=limit)


@app.command("topvoted")
def topvoted(
    n: int = typer.Option(10, "--N", "--n", help="Number of entries to show."),
    repo: str | None = typer.Option(
        None,
        "--repo",
        help="GitHub repo owner/name. Defaults to CUHKVOTING_REPO or current git remote.",
    ),
    branch: str = typer.Option(
        os.getenv("CUHKVOTING_BRANCH", "main"),
        "--branch",
        help="Git branch to read/write.",
    ),
) -> None:
    _run_cmd(cmd_topvoted, N=n, repo=repo, branch=branch)


@app.command("vote")
def vote(
    paper_id: str = typer.Argument(..., help="arXiv id/url."),
    repo: str | None = typer.Option(
        None,
        "--repo",
        help="GitHub repo owner/name. Defaults to CUHKVOTING_REPO or current git remote.",
    ),
    branch: str = typer.Option(
        os.getenv("CUHKVOTING_BRANCH", "main"),
        "--branch",
        help="Git branch to read/write.",
    ),
) -> None:
    _run_cmd(cmd_vote, paper_id=paper_id, repo=repo, branch=branch)


def main() -> None:
    app()
