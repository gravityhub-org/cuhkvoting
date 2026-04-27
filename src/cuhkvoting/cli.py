from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass


ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_ABS = "https://arxiv.org/abs/"


@dataclass
class RepoConfig:
    owner: str
    repo: str
    branch: str
    token: str | None


def _http_json(url: str, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_text(url: str, headers: dict[str, str] | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _http_put_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
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


def _derive_repo_from_git() -> tuple[str, str] | None:
    try:
        out = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return _parse_repo_url(out)
    except Exception:
        return None
    return None


def _get_github_username() -> str | None:
    env_user = os.getenv("GITHUB_USER")
    if env_user:
        return env_user
    try:
        out = subprocess.check_output(
            ["gh", "api", "user", "--jq", ".login"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out or None
    except Exception:
        return None


def _get_github_token() -> str | None:
    return os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or _gh_token()


def _gh_token() -> str | None:
    try:
        out = subprocess.check_output(
            ["gh", "auth", "token"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out or None
    except Exception:
        return None


def _resolve_repo_config(args: argparse.Namespace) -> RepoConfig:
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
            owner, repo = parsed
    if not owner or not repo:
        raise SystemExit(
            "Could not determine GitHub repo. Set CUHKVOTING_REPO=owner/name or pass --repo owner/name."
        )
    return RepoConfig(owner=owner, repo=repo, branch=args.branch, token=_get_github_token())


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


def _github_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "cuhkvoting/0.1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _list_paper_paths(cfg: RepoConfig) -> list[str]:
    url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}/git/trees/{cfg.branch}?recursive=1"
    try:
        data = _http_json(url, headers=_github_headers(cfg.token))
    except urllib.error.HTTPError as e:
        if e.code in (404, 409):
            return []
        raise
    tree = data.get("tree", [])
    paths = [obj["path"] for obj in tree if obj.get("type") == "blob" and obj.get("path", "").startswith("papers/") and obj.get("path", "").endswith(".json")]
    return sorted(paths)


def _get_paper(cfg: RepoConfig, path: str) -> tuple[dict, str | None]:
    url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}/contents/{path}?ref={cfg.branch}"
    data = _http_json(url, headers=_github_headers(cfg.token))
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data.get("sha")


def _put_paper(cfg: RepoConfig, path: str, body: dict, message: str, sha: str | None) -> None:
    if not cfg.token:
        raise SystemExit(
            "Voting requires GitHub auth. Set GITHUB_TOKEN/GH_TOKEN or run `gh auth login`."
        )
    url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}/contents/{path}"
    payload = {
        "message": message,
        "branch": cfg.branch,
        "content": base64.b64encode((json.dumps(body, indent=2, sort_keys=True) + "\n").encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    _http_put_json(url, payload, headers=_github_headers(cfg.token))


def cmd_today(args: argparse.Namespace) -> int:
    today = dt.datetime.utcnow().strftime("%Y%m%d")
    params = {
        "search_query": f"submittedDate:[{today}0000 TO {today}2359]",
        "start": "0",
        "max_results": str(args.limit),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    entries = _arxiv_query(params)
    if not entries:
        print("No papers found for today (UTC).")
        return 0
    for idx, p in enumerate(entries, 1):
        print(f"{idx:>2}. {p['id']}  {p['title']}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
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


def cmd_topvoted(args: argparse.Namespace) -> int:
    cfg = _resolve_repo_config(args)
    paths = _list_paper_paths(cfg)
    papers: list[dict] = []
    for path in paths:
        try:
            paper, _ = _get_paper(cfg, path)
            votes = paper.get("votes", [])
            papers.append(
                {
                    "id": paper.get("id", path.rsplit("/", 1)[-1].replace(".json", "")),
                    "title": paper.get("title", "(no title)"),
                    "votes": len(votes),
                }
            )
        except Exception:
            continue
    papers.sort(key=lambda p: (-p["votes"], p["id"]))
    topn = papers[: args.N]
    if not topn:
        print("No voted papers yet.")
        return 0
    for idx, p in enumerate(topn, 1):
        print(f"{idx:>2}. [{p['votes']:>3} votes] {p['id']}  {p['title']}")
    return 0


def cmd_vote(args: argparse.Namespace) -> int:
    cfg = _resolve_repo_config(args)
    user = _get_github_username()
    if not user:
        raise SystemExit("Could not identify GitHub user. Set GITHUB_USER or run `gh auth login`.")

    paper_id = _normalize_paper_id(args.paper_id)
    path = f"papers/{_safe_filename(paper_id)}.json"

    try:
        paper, sha = _get_paper(cfg, path)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        # Paper not tracked yet: fetch metadata from arXiv.
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
    votes.append({"user": user, "voted_at": dt.datetime.utcnow().isoformat() + "Z"})

    _put_paper(
        cfg,
        path,
        paper,
        message=f"vote: {user} -> {paper.get('id', paper_id)}",
        sha=sha,
    )
    print(f"Vote recorded: {user} -> {paper.get('id', paper_id)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cuhkvoting",
        description="Minimal arXiv voting CLI backed by GitHub.",
    )
    parser.add_argument(
        "--repo",
        help="GitHub repo in owner/name format. Defaults to CUHKVOTING_REPO or current git remote.",
    )
    parser.add_argument(
        "--branch",
        default=os.getenv("CUHKVOTING_BRANCH", "main"),
        help="Git branch to read/write (default: main or CUHKVOTING_BRANCH).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_today = sub.add_parser("today", help="Show today's arXiv submissions (UTC).")
    p_today.add_argument("--limit", type=int, default=20)
    p_today.set_defaults(func=cmd_today)

    p_search = sub.add_parser("search", help="Search arXiv by keyword.")
    p_search.add_argument("query", help="Search terms.")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    p_top = sub.add_parser("topvoted", help="List top voted tracked papers.")
    p_top.add_argument("--N", "--n", type=int, default=10, dest="N")
    p_top.set_defaults(func=cmd_topvoted)

    p_vote = sub.add_parser("vote", help="Vote once per GitHub user for one paper.")
    p_vote.add_argument("paper_id", help="arXiv id/url.")
    p_vote.set_defaults(func=cmd_vote)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        code = args.func(args)
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="ignore")
        raise SystemExit(f"HTTP {e.code}: {msg[:300]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Network error: {e.reason}")
    sys.exit(code)
