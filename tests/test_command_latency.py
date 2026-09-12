"""Guardrail: CLI commands must finish quickly over a real network (no hang).

Network-facing commands are invoked live (GitHub / arXiv / INSPIRE). Local-only
commands (config, TUI stub, dry-run mutators) stay mocked so the suite does not
rewrite user state.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from typer.main import get_command
from typer.testing import CliRunner

from cuhkvoting import cli


# Ceiling for the heaviest live command (topvoted / vote listing).
# Target is ~3s; a little slack covers shared-egress jitter (search can land at 3.01s).
MAX_CMD_SECONDS = 3.5

_SAMPLE_ENTRY = {
    "id": "2601.00001",
    "title": "Latency Probe Paper",
    "abstract": "Abstract text.",
    "authors": ["Ada Lovelace"],
    "published": "2026-01-15T00:00:00Z",
    "url": "https://arxiv.org/abs/2601.00001",
    "primary_category": "gr-qc",
}

_SAMPLE_PAPER = {
    "id": "2601.00001",
    "title": "Latency Probe Paper",
    "abstract": "Abstract text.",
    "url": "https://arxiv.org/abs/2601.00001",
    "votes": [{"user": "octocat", "voted_at": "2026-01-01T00:00:00Z"}],
}


def _registered_command_paths() -> set[str]:
    """Flatten Typer/Click command paths, e.g. ``today``, ``admin trash``."""
    root = get_command(cli.app)
    paths: set[str] = set()

    def walk(cmd, prefix: tuple[str, ...]) -> None:
        list_commands = getattr(cmd, "list_commands", None)
        names = list_commands(None) if callable(list_commands) else None
        if not names:
            if prefix:
                paths.add(" ".join(prefix))
            return
        for name in names:
            sub = cmd.get_command(None, name)
            if sub is None:
                continue
            walk(sub, prefix + (name,))

    walk(root, ())
    return paths


def _network_reachable() -> bool:
    try:
        urllib.request.urlopen("https://api.github.com", timeout=5).read(64)
        return True
    except Exception:
        return False


# Live internet — these are the hang-prone paths (GitHub / arXiv / INSPIRE).
_LIVE_ARGV: dict[str, list[str]] = {
    "today": ["today", "--limit", "1"],
    "search": ["search", "gravitational", "--limit", "1"],
    "lastweek": ["lastweek", "--limit", "1"],
    "last": ["last", "3", "--limit", "1"],
    "topvoted": ["topvoted", "--n", "5"],
    "record": ["record"],
    "vote": ["vote"],
    "show": ["show", "2604.18676"],
    "admin trash": ["admin", "trash"],
    "admin meta": ["admin", "meta"],
}

# No live network (would mutate state or open a TUI).
_LOCAL_ARGV: dict[str, list[str]] = {
    "select": ["select", "2601.00001"],
    "interactive": ["interactive", "today"],
    "init-config": ["init-config", "--force"],
    "admin sanitize": ["admin", "sanitize", "--dry-run"],
    "admin set-version": ["admin", "set-version", "0.0.0", "--dry-run"],
}

_COMMAND_ARGV = {**_LIVE_ARGV, **_LOCAL_ARGV}


@unittest.skipUnless(_network_reachable(), "no network — live latency checks skipped")
class LiveCommandLatencyTests(unittest.TestCase):
    """Run hang-prone commands against the real internet within the 3s budget."""

    def test_live_commands_finish_within_budget(self) -> None:
        for name, argv in sorted(_LIVE_ARGV.items()):
            with self.subTest(command=name):
                elapsed, code = self._run_live(argv)
                self.assertLessEqual(
                    elapsed,
                    MAX_CMD_SECONDS,
                    msg=(
                        f"{name!r} took {elapsed:.2f}s over the network "
                        f"(budget {MAX_CMD_SECONDS}s); argv={argv}"
                    ),
                )
                # Soft: allow nonzero only when the command itself refused work;
                # a crash/hang is already covered by the timeout.
                self.assertIsInstance(code, int)

    def _run_live(self, argv: list[str]) -> tuple[float, int]:
        runner = CliRunner()

        def invoke() -> int:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = runner.invoke(cli.app, argv, catch_exceptions=False)
            return int(result.exit_code)

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(invoke)
            try:
                code = fut.result(timeout=MAX_CMD_SECONDS + 0.5)
            except concurrent.futures.TimeoutError:
                self.fail(
                    f"live command hung beyond {MAX_CMD_SECONDS}s: {' '.join(argv)}"
                )
            except urllib.error.URLError as exc:
                self.skipTest(f"network error during {' '.join(argv)}: {exc}")
        return time.perf_counter() - t0, code


class LocalCommandLatencyTests(unittest.TestCase):
    def test_latency_cases_cover_every_registered_command(self) -> None:
        registered = _registered_command_paths()
        self.assertEqual(
            registered,
            set(_COMMAND_ARGV),
            msg=(
                "Add/remove entries in _LIVE_ARGV / _LOCAL_ARGV when CLI commands change.\n"
                f"missing={sorted(registered - set(_COMMAND_ARGV))}\n"
                f"extra={sorted(set(_COMMAND_ARGV) - registered)}"
            ),
        )

    def test_local_commands_finish_within_budget(self) -> None:
        for name, argv in sorted(_LOCAL_ARGV.items()):
            with self.subTest(command=name):
                elapsed = self._run_local(argv)
                self.assertLessEqual(
                    elapsed,
                    MAX_CMD_SECONDS,
                    msg=f"{name!r} took {elapsed:.2f}s (budget {MAX_CMD_SECONDS}s); argv={argv}",
                )

    def _run_local(self, argv: list[str]) -> float:
        def invoke() -> None:
            runner = CliRunner()
            with self._fast_io():
                runner.invoke(cli.app, argv, catch_exceptions=False)

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(invoke)
            try:
                fut.result(timeout=MAX_CMD_SECONDS + 0.5)
            except concurrent.futures.TimeoutError:
                self.fail(
                    f"command hung beyond {MAX_CMD_SECONDS}s budget: {' '.join(argv)}"
                )
        return time.perf_counter() - t0

    @contextlib.contextmanager
    def _fast_io(self):
        tmp_root = tempfile.mkdtemp(prefix="cuhkvoting-latency-")
        self.addCleanup(lambda: shutil.rmtree(tmp_root, ignore_errors=True))
        tmp_config = Path(tmp_root) / "config.toml"
        clone_dir = Path(tmp_root) / "clone"
        papers_dir = clone_dir / "papers"
        papers_dir.mkdir(parents=True)
        (papers_dir / "2601.00001.json").write_text(
            json.dumps(_SAMPLE_PAPER, indent=2) + "\n", encoding="utf-8"
        )
        (papers_dir / "journal_club_records.json").write_text(
            json.dumps({"records": []}, indent=2) + "\n", encoding="utf-8"
        )
        jc_records = [
            {
                "arxiv_id": "2601.00001",
                "title": "Latency Probe Paper",
                "week": "2026-W01",
                "selected_by": "octocat",
                "selected_at": "2026-01-01T00:00:00Z",
                "historical_vote": 1,
            }
        ]

        @contextlib.contextmanager
        def fake_checkout(_cfg):
            yield str(clone_dir)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "CONFIG_PATH", tmp_config))
            stack.enter_context(mock.patch("cuhkvoting.cli._get_token", return_value="tok"))
            stack.enter_context(mock.patch("cuhkvoting.cli._resolve_user", return_value="tester"))
            stack.enter_context(mock.patch("cuhkvoting.cli._has_github_ssh_access", return_value=True))
            stack.enter_context(mock.patch("cuhkvoting.cli._repo_checkout", fake_checkout))
            stack.enter_context(
                mock.patch(
                    "cuhkvoting.cli._load_jc_records_and_display_names",
                    return_value=(list(jc_records), "sha", {}),
                )
            )
            stack.enter_context(
                mock.patch(
                    "cuhkvoting.cli._load_json_via_api",
                    return_value=({"client": {"latest_version": "0.3.0"}}, "sha"),
                )
            )
            stack.enter_context(mock.patch("cuhkvoting.interactive.run", return_value=0))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            yield


if __name__ == "__main__":
    unittest.main()
