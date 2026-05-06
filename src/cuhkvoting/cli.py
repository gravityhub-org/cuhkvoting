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
INSPIRE_API = "https://inspirehep.net/api/literature"
DEFAULT_REPO = "gravityhub-org/cuhkvoting-records"
VOTE_EXPIRY_DAYS = 183
JC_RECORD_PATH = "papers/journal_club_records.json"


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


def _http_delete_json(url: str, payload: dict, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers or {},
        method="DELETE",
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
    else:
        parsed = _derive_repo_from_git()
        if parsed:
            detected_owner, detected_repo, _remote_url = parsed
            if (detected_owner, detected_repo) != (owner, repo):
                raise SystemExit(
                    f"Current git remote points to {detected_owner}/{detected_repo}. "
                    f"Only {DEFAULT_REPO} is supported. Pass --repo {DEFAULT_REPO} or set CUHKVOTING_REPO."
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
    _http_put_json(url, payload, headers=_github_headers(token))


def _save_json_via_api(cfg: RepoConfig, path: str, body: dict, sha: str | None, token: str, message: str) -> None:
    _save_paper_via_api(cfg, path, body, sha, token, message)


def _delete_paper_via_api(cfg: RepoConfig, path: str, sha: str, token: str, message: str) -> None:
    url = f"https://api.github.com/repos/{cfg.owner}/{cfg.repo}/contents/{path}"
    payload = {"message": message, "branch": cfg.branch, "sha": sha}
    _http_delete_json(url, payload, headers=_github_headers(token))


def _load_jc_records(cfg: RepoConfig, token: str | None) -> tuple[list[dict], str | None]:
    data, sha = _load_json_via_api(cfg, JC_RECORD_PATH, token)
    if data is not None:
        records = data.get("records", [])
        return (records if isinstance(records, list) else []), sha
    if _has_github_ssh_access():
        clone_dir, cleanup_dir = _with_repo_checkout(cfg)
        try:
            p = Path(clone_dir) / JC_RECORD_PATH
            if p.exists():
                body = json.loads(p.read_text(encoding="utf-8"))
                records = body.get("records", [])
                return (records if isinstance(records, list) else []), None
        finally:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
    return [], None


def _save_jc_records(cfg: RepoConfig, token: str | None, user: str, records: list[dict], sha: str | None, message: str) -> None:
    body = {"records": records}
    if token:
        _save_json_via_api(cfg, JC_RECORD_PATH, body, sha, token, message)
        return
    if not _has_github_ssh_access():
        raise SystemExit(f"Writing records needs auth. Set CUHKVOTING_TOKEN/GITHUB_TOKEN or configure SSH key.\n\n{_ssh_setup_instructions()}")
    clone_dir, cleanup_dir = _with_repo_checkout(cfg)
    try:
        p = Path(clone_dir) / JC_RECORD_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _ensure_commit_identity(clone_dir, user)
        _run_git(["add", JC_RECORD_PATH], cwd=clone_dir)
        _run_git(["commit", "-m", message], cwd=clone_dir)
        _run_git(["push", "origin", f"HEAD:{cfg.branch}"], cwd=clone_dir)
    finally:
        shutil.rmtree(cleanup_dir, ignore_errors=True)


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
        authors: list[str] = []
        for author in ent.findall("atom:author", ns):
            full_name = " ".join((author.findtext("atom:name", "", ns) or "").split())
            if full_name:
                authors.append(full_name)
        arxiv_id = _strip_arxiv_version(entry_id.rsplit("/", 1)[-1])
        entries.append(
            {
                "id": arxiv_id,
                "title": title,
                "abstract": summary,
                "url": f"{ARXIV_ABS}{arxiv_id}",
                "authors": authors,
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
    data = _http_json(url, headers={"User-Agent": "cuhkvoting/0.1"})
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


def _format_voters(votes: list[dict]) -> str:
    voters = [str(v.get("user", "")).strip() for v in votes]
    voters = [v for v in voters if v]
    if not voters:
        return "-"
    return ", ".join(sorted(set(voters), key=str.lower))


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
    if paper is None and _has_github_ssh_access():
        clone_dir, cleanup_dir = _with_repo_checkout(cfg)
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
            shutil.rmtree(cleanup_dir, ignore_errors=True)
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
    clone_dir, cleanup_dir = _with_repo_checkout(cfg)
    try:
        paper_file = Path(clone_dir) / save_path
        paper_file.parent.mkdir(parents=True, exist_ok=True)
        paper_file.write_text(json.dumps(paper, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _ensure_commit_identity(clone_dir, user)
        _run_git(["add", str(Path(save_path))], cwd=clone_dir)
        _run_git(["commit", "-m", message], cwd=clone_dir)
        _run_git(["push", "origin", f"HEAD:{cfg.branch}"], cwd=clone_dir)
    finally:
        shutil.rmtree(cleanup_dir, ignore_errors=True)


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
    clone_dir, cleanup_dir = _with_repo_checkout(cfg)
    try:
        paper_file = Path(clone_dir) / save_path
        if not paper_file.exists():
            raise SystemExit(f"Vote file not found for deletion: {save_path}")
        _ensure_commit_identity(clone_dir, user)
        _run_git(["rm", str(Path(save_path))], cwd=clone_dir)
        _run_git(["commit", "-m", message], cwd=clone_dir)
        _run_git(["push", "origin", f"HEAD:{cfg.branch}"], cwd=clone_dir)
    finally:
        shutil.rmtree(cleanup_dir, ignore_errors=True)


def _validate_arxiv_entry(paper_id: str) -> dict:
    entries = _arxiv_query({"search_query": f"id:{paper_id}", "start": "0", "max_results": "1"})
    if not entries:
        raise SystemExit(f"Could not find arXiv entry for id '{paper_id}'.")
    return entries[0]


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
    fetch_limit = max(int(args.limit), 200) if getattr(args, "keywords", None) else int(args.limit)
    params = {
        "search_query": f"submittedDate:[{start} TO {end}]",
        "start": "0",
        "max_results": str(fetch_limit),
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
                "max_results": str(fetch_limit),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        if not entries:
            print("No papers found for today (UTC).")
            return 0
        print("No new UTC-day submissions. Showing most recent arXiv entries:")
    entries = _filter_entries(entries, getattr(args, "keywords", None))
    if not entries:
        print("No papers matched keyword filter.")
        return 0
    for idx, p in enumerate(entries[: int(args.limit)], 1):
        lastnames = _format_author_lastnames(p.get("authors", []), max_authors=3)
        print(f"{idx:>2}. {_format_clickable_id(p['id'])}  {p['title']}  [{lastnames}]")
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
    for idx, p in enumerate(entries[:requested_limit], 1):
        pid = str(p.get("id", ""))
        print(f"{idx:>2}. {_format_clickable_id(pid)}  {p['title']}")
    return 0


def cmd_lastweek(args: SimpleNamespace) -> int:
    end_day = dt.datetime.utcnow().date()
    start_day = end_day - dt.timedelta(days=7)
    start = start_day.strftime("%Y%m%d")
    end = end_day.strftime("%Y%m%d")
    fetch_limit = max(int(args.limit), 300) if getattr(args, "keywords", None) else int(args.limit)
    params = {
        "search_query": f"(cat:gr-qc OR cat:astro-ph.*) AND submittedDate:[{start}0000 TO {end}2359]",
        "start": "0",
        "max_results": str(fetch_limit),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    entries = _arxiv_query(params)
    entries = _filter_entries(entries, getattr(args, "keywords", None))
    if not entries:
        print("No gr-qc / astro-ph entries matched in last week (UTC).")
        return 0
    for idx, p in enumerate(entries[: int(args.limit)], 1):
        lastnames = _format_author_lastnames(p.get("authors", []), max_authors=3)
        print(f"{idx:>2}. {_format_clickable_id(p['id'])}  {p['title']}  [{lastnames}]")
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
        _prune_expired_votes(paper)
        if paper.get("selected"):
            continue
        votes = paper.get("votes", [])
        vote_count = len(votes)
        if vote_count <= 0:
            continue
        rows.append(
            {
                "id": _strip_arxiv_version(str(paper.get("id", "(unknown)"))),
                "title": paper.get("title", "(no title)"),
                "votes": vote_count,
                "voters": _format_voters(votes),
            }
        )
    rows.sort(key=lambda p: (-p["votes"], p["id"]))
    topn = rows[: args.N]
    if not topn:
        print("No voted papers yet.")
        return 0
    for idx, p in enumerate(topn, 1):
        print(f"{idx:>2}. {_format_clickable_id(p['id'])}  {p['title']} [{p['voters']}]")
    return 0


def cmd_vote(args: SimpleNamespace) -> int:
    cfg = _resolve_repo_config(args)
    token = _get_token()
    user = _resolve_user(token)
    paper_id = _normalize_paper_id(args.paper_id)
    validate_entry = _validate_arxiv_entry(paper_id)
    paper, sha, save_path = _load_vote_paper(cfg, token, paper_id)

    if paper is None:
        entry = validate_entry
        paper = {
            "id": entry["id"],
            "title": entry["title"],
            "abstract": entry["abstract"],
            "url": entry["url"],
            "votes": [],
        }

    _prune_expired_votes(paper)
    votes = paper.setdefault("votes", [])
    if any(v.get("user") == user for v in votes):
        raise SystemExit(f"User '{user}' already voted for {paper.get('id', paper_id)}.")
    paper_vote_id = _strip_arxiv_version(str(paper.get("id", paper_id)))
    paper["id"] = paper_vote_id
    votes.append({"user": user, "voted_at": dt.datetime.utcnow().isoformat() + "Z"})
    _save_vote_paper(cfg, token, user, paper, sha, save_path, f"vote: {user} -> {paper_vote_id}")
    print(f"Vote recorded: {user} -> {paper_vote_id}")
    return 0


def cmd_vote_remove(args: SimpleNamespace) -> int:
    cfg = _resolve_repo_config(args)
    token = _get_token()
    user = _resolve_user(token)
    paper_id = _normalize_paper_id(args.paper_id)
    _validate_arxiv_entry(paper_id)
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
) -> None:
    _run_cmd(cmd_today, limit=limit, keywords=keywords)


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
) -> None:
    _run_cmd(cmd_lastweek, limit=limit, keywords=keywords)


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
) -> None:
    _run_cmd(cmd_topvoted, N=n, repo=repo, branch=branch)


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
        help="One or more arXiv ids/urls OR action `remove <id>` (use top-level `select <id>`).",
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
        _run_cmd(cmd_vote_remove, paper_id=action_or_paper[1], repo=repo, branch=branch)
        return
    if action == "select":
        if len(action_or_paper) < 2:
            raise typer.BadParameter("Usage: cuhkvoting select <id>")
        if len(action_or_paper) > 2:
            raise typer.BadParameter("Usage: cuhkvoting select <id>")
        _run_cmd(cmd_select, paper_id=action_or_paper[1], repo=repo, branch=branch)
        return
    last_code = 0
    for one_paper_id in action_or_paper:
        last_code = _invoke_cmd(cmd_vote, paper_id=one_paper_id, repo=repo, branch=branch)
        if last_code != 0:
            raise typer.Exit(code=last_code)
    raise typer.Exit(code=last_code)


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


app.add_typer(admin_app, name="admin")


def main() -> None:
    app()
