"""Unit tests for the profile README updater.

These tests never touch the network. The GitHub client is exercised with a fake
opener, and every renderer is a pure function over explicit fixtures.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import update_profile as up  # noqa: E402

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def make_repo(**overrides: object) -> up.Repo:
    defaults: dict[str, object] = {
        "name": "sample-repo",
        "owner": "faseehabdullah57-blip",
        "description": None,
        "url": "https://github.com/faseehabdullah57-blip/sample-repo",
        "language": "Python",
        "stars": 0,
        "forks": 0,
        "updated_at": "2026-11-13T06:31:06Z",
        "pushed_at": "2026-11-13T06:31:06Z",
        "default_branch": "main",
        "tree_paths": ["README.md", "src/main.py"],
    }
    defaults.update(overrides)
    return up.Repo(**defaults)  # type: ignore[arg-type]


def make_commit(
    repo: str = "sample-repo",
    message: str = "feat: add idempotent webhook handling",
    when: datetime = NOW - timedelta(days=1),
) -> up.Commit:
    return up.Commit(
        repo=repo,
        repo_url=f"https://github.com/faseehabdullah57-blip/{repo}",
        sha="a" * 40,
        short_sha="aaaaaaa",
        message=message,
        date=when,
        url=f"https://github.com/faseehabdullah57-blip/{repo}/commit/{'a' * 40}",
    )


def make_release(
    repo: str = "sample-repo",
    tag: str = "v1.0.0",
    when: datetime = NOW - timedelta(days=10),
    prerelease: bool = False,
) -> up.Release:
    return up.Release(
        repo=repo,
        tag=tag,
        name=tag,
        published=when,
        url=f"https://github.com/faseehabdullah57-blip/{repo}/releases/tag/{tag}",
        prerelease=prerelease,
    )


class SectionTests(unittest.TestCase):
    def test_replaces_exactly_one_section(self) -> None:
        text = "manual before\n<!-- x:start -->\nold\n<!-- x:end -->\nmanual after"
        result = up.replace_section(text, "x", "new body")
        self.assertEqual(
            result,
            "manual before\n<!-- x:start -->\n\nnew body\n\n<!-- x:end -->\nmanual after",
        )

    def test_preserves_manual_content_outside_markers(self) -> None:
        text = "# Title\n\n<!-- x:start -->old<!-- x:end -->\n\nFooter"
        result = up.replace_section(text, "x", "generated")
        self.assertIn("# Title", result)
        self.assertIn("Footer", result)
        self.assertIn("generated", result)
        self.assertNotIn("old", result)

    def test_missing_end_marker_raises(self) -> None:
        with self.assertRaises(up.SectionError):
            up.replace_section("<!-- x:start -->\nbody", "x", "new")

    def test_missing_start_marker_raises(self) -> None:
        with self.assertRaises(up.SectionError):
            up.replace_section("body\n<!-- x:end -->", "x", "new")

    def test_duplicate_marker_raises(self) -> None:
        text = "<!-- x:start -->a<!-- x:end -->\n<!-- x:start -->b<!-- x:end -->"
        with self.assertRaises(up.SectionError):
            up.replace_section(text, "x", "new")

    def test_end_before_start_raises(self) -> None:
        with self.assertRaises(up.SectionError):
            up.replace_section("<!-- x:end -->\nbody\n<!-- x:start -->", "x", "new")

    def test_empty_body_gets_safe_placeholder(self) -> None:
        text = "<!-- x:start -->\nold\n<!-- x:end -->"
        self.assertIn("_No data available._", up.replace_section(text, "x", "  "))

    def test_body_with_markdown_is_inserted_literally(self) -> None:
        text = "<!-- x:start -->\nold\n<!-- x:end -->"
        body = "| a | b |\n| - | - |\n`code`"
        self.assertIn(body, up.replace_section(text, "x", body))


class DateAndEscapingTests(unittest.TestCase):
    def test_format_date_utc(self) -> None:
        self.assertEqual(up.format_date("2026-11-13T06:31:06Z"), "2026-11-13")

    def test_format_date_offset_is_normalized_to_utc(self) -> None:
        self.assertEqual(up.format_date("2026-11-14T01:00:00+05:00"), "2026-11-13")

    def test_format_date_invalid(self) -> None:
        self.assertEqual(up.format_date(None), "unknown")
        self.assertEqual(up.format_date("not-a-date"), "unknown")

    def test_sanitize_removes_comments_controls_and_newlines(self) -> None:
        value = "hello\u0000\n<!-- recent-activity:start -->world"
        result = up.sanitize_inline(value)
        self.assertNotIn("<!--", result)
        self.assertNotIn("\n", result)
        self.assertEqual(result, "hello world")

    def test_escape_markdown_escapes_table_pipes_and_html(self) -> None:
        result = up.escape_markdown("A | B <script> *bold* `code`")
        self.assertIn(r"\|", result)
        self.assertIn("&lt;script&gt;", result)
        self.assertIn(r"\*bold\*", result)
        self.assertIn(r"\`code\`", result)

    def test_escape_markdown_truncates(self) -> None:
        result = up.escape_markdown("x" * 100, limit=20)
        self.assertTrue(result.endswith("..."))
        self.assertLessEqual(len(result), 20)


class RepoSignalTests(unittest.TestCase):
    def test_has_tests_for_common_layouts(self) -> None:
        self.assertTrue(make_repo(tree_paths=["tests/test_validator.py"]).has_tests)
        self.assertTrue(make_repo(tree_paths=["src/foo.test.js"]).has_tests)
        self.assertTrue(make_repo(tree_paths=["src/foo.spec.ts"]).has_tests)
        self.assertTrue(make_repo(tree_paths=["test_main.py"]).has_tests)
        self.assertTrue(make_repo(tree_paths=["src/main_test.py"]).has_tests)
        self.assertFalse(make_repo(tree_paths=["src/testing.py"]).has_tests)

    def test_ci_docker_and_readme_detection(self) -> None:
        repo = make_repo(
            tree_paths=[
                "README.md",
                "Dockerfile",
                ".github/workflows/ci.yml",
            ]
        )
        self.assertTrue(repo.has_ci)
        self.assertTrue(repo.has_docker)
        self.assertTrue(repo.has_readme)
        self.assertFalse(make_repo(tree_paths=["docs/README.md"]).has_readme)

    def test_latest_commit_and_release(self) -> None:
        older = make_commit(when=NOW - timedelta(days=10))
        newer = make_commit(when=NOW - timedelta(days=1))
        repo = make_repo(
            commits=[older, newer],
            releases=[make_release(tag="v1"), make_release(tag="v2", when=NOW)],
        )
        self.assertEqual(repo.latest_commit, newer)
        self.assertEqual(repo.latest_release.tag, "v2")


class RankingTests(unittest.TestCase):
    def test_recent_repo_outranks_stale_repo(self) -> None:
        recent = make_repo(name="recent", pushed_at="2026-11-20T00:00:00Z")
        stale = make_repo(name="stale", pushed_at="2024-01-01T00:00:00Z")
        ranked = up.rank_repositories([stale, recent], NOW)
        self.assertEqual(ranked[0].name, "recent")

    def test_tests_and_ci_increase_score(self) -> None:
        plain = make_repo(name="plain")
        engineered = make_repo(
            name="engineered",
            tree_paths=["README.md", "tests/test_x.py", ".github/workflows/ci.yml"],
        )
        self.assertGreater(
            up.repository_score(engineered, NOW), up.repository_score(plain, NOW)
        )

    def test_alphabetical_tie_break(self) -> None:
        alpha = make_repo(name="alpha")
        beta = make_repo(name="beta")
        ranked = up.rank_repositories([beta, alpha], NOW)
        self.assertEqual([repo.name for repo in ranked], ["alpha", "beta"])


class FakePagedClient:
    def __init__(self, pages: list[object], token: str | None = "token") -> None:
        self.pages = pages
        self.token = token
        self.paths: list[str] = []

    def get(self, path: str, *, allow_missing: bool = False) -> object:
        self.paths.append(path)
        return self.pages.pop(0) if self.pages else []


class DiscoveryTests(unittest.TestCase):
    def test_excludes_private_forks_profile_and_duplicates(self) -> None:
        client = FakePagedClient(
            [
                [
                    {"name": "keep", "private": False, "fork": False},
                    {"name": "private", "private": True, "fork": False},
                    {"name": "fork", "private": False, "fork": True},
                    {"name": "faseehabdullah57-blip", "private": False, "fork": False},
                    {"name": "keep", "private": False, "fork": False},
                ]
            ]
        )
        result = up.discover_public_repositories(
            client, "faseehabdullah57-blip", "faseehabdullah57-blip"
        )
        self.assertEqual([item["name"] for item in result], ["keep"])

    def test_public_owner_endpoint_is_used_even_with_token(self) -> None:
        client = FakePagedClient([[]], token="token")
        up.discover_public_repositories(client, "owner", "profile")
        self.assertTrue(client.paths[0].startswith("/users/owner/repos"))

    def test_unauthenticated_endpoint_is_used(self) -> None:
        client = FakePagedClient([[]], token=None)
        up.discover_public_repositories(client, "owner", "profile")
        self.assertTrue(client.paths[0].startswith("/users/owner/repos"))

    def test_pagination_stops_on_short_page(self) -> None:
        first_page = [{"name": f"repo-{i}"} for i in range(up.PER_PAGE)]
        second_page = [{"name": "last-repo"}]
        client = FakePagedClient([first_page, second_page])
        result = up.discover_public_repositories(client, "owner", "profile")
        self.assertEqual(len(result), up.PER_PAGE + 1)
        self.assertEqual(len(client.paths), 2)

    def test_empty_discovery(self) -> None:
        client = FakePagedClient([[]])
        self.assertEqual(
            up.discover_public_repositories(client, "owner", "profile"), []
        )


class FakeRepoClient:
    def __init__(
        self,
        data: dict[str, object],
        failures: set[str] | None = None,
        stats_sequence: list[tuple[int, object]] | None = None,
    ) -> None:
        self.data = data
        self.failures = failures or set()
        self.stats_sequence = stats_sequence
        self.sleeps: list[float] = []
        self.request_calls: list[str] = []

    def get(self, path: str, *, allow_missing: bool = False) -> object:
        if path in self.failures:
            raise up.GitHubError(f"boom: {path}", status=500)
        return self.data.get(path)

    def request(self, path: str, *, allow_missing: bool = False) -> tuple[int, object]:
        self.request_calls.append(path)
        if path in self.failures:
            raise up.GitHubError(f"boom: {path}", status=500)
        if self.stats_sequence is not None:
            return self.stats_sequence.pop(0)
        return 200, self.data.get(path)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class CollectionTests(unittest.TestCase):
    def base_path(self) -> str:
        return "/repos/faseehabdullah57-blip/sample-repo"

    def test_collects_real_signals(self) -> None:
        base = self.base_path()
        client = FakeRepoClient(
            {
                f"{base}/languages": {"Python": 120, "Dockerfile": 10},
                f"{base}/releases?per_page=10": [
                    {"tag_name": "v1", "name": "v1", "published_at": "2026-11-01T00:00:00Z", "draft": False, "prerelease": False, "html_url": "https://github.com/o/r/releases/tag/v1"},
                    {"tag_name": "draft", "name": "draft", "published_at": "2026-11-02T00:00:00Z", "draft": True},
                ],
                f"{base}/commits?per_page=10": [
                    {"sha": "b" * 40, "html_url": "https://github.com/o/r/commit/b", "commit": {"message": "feat: thing\n\nbody", "committer": {"date": "2026-11-13T00:00:00Z"}}}
                ],
                f"{base}/git/trees/main?recursive=1": {
                    "tree": [
                        {"path": "README.md", "type": "blob"},
                        {"path": "Dockerfile", "type": "blob"},
                        {"path": "tests/test_x.py", "type": "blob"},
                        {"path": "src", "type": "tree"},
                    ],
                    "truncated": False,
                },
                f"{base}/stats/commit_activity": [
                    {"week": 1762560000, "days": [0, 1, 0, 2, 0, 0, 0]}
                ],
            }
        )
        repo = up.collect_repository(
            client, "faseehabdullah57-blip", {"name": "sample-repo", "owner": {"login": "faseehabdullah57-blip"}}
        )
        self.assertEqual(repo.languages, {"Python": 120, "Dockerfile": 10})
        self.assertEqual(len(repo.releases), 1)
        self.assertEqual(repo.commits[0].message, "feat: thing")
        self.assertTrue(repo.has_tests)
        self.assertTrue(repo.has_docker)
        self.assertTrue(repo.has_readme)
        self.assertEqual(len(repo.activity_weeks), 1)
        self.assertEqual(repo.errors, [])

    def test_one_endpoint_failure_does_not_abort(self) -> None:
        base = self.base_path()
        client = FakeRepoClient(
            {
                f"{base}/commits?per_page=10": [
                    {"sha": "c" * 40, "commit": {"message": "still here", "committer": {"date": "2026-11-13T00:00:00Z"}}}
                ],
                f"{base}/git/trees/main?recursive=1": {"tree": [], "truncated": False},
                f"{base}/stats/commit_activity": [],
            },
            failures={f"{base}/releases?per_page=10", f"{base}/languages"},
        )
        repo = up.collect_repository(
            client,
            "faseehabdullah57-blip",
            {"name": "sample-repo", "owner": {"login": "faseehabdullah57-blip"}},
        )
        self.assertEqual(repo.releases, [])
        self.assertEqual(repo.languages, {})
        self.assertEqual(repo.commits[0].message, "still here")
        self.assertEqual(len(repo.errors), 2)

    def test_stats_202_is_retried(self) -> None:
        base = self.base_path()
        client = FakeRepoClient(
            {},
            stats_sequence=[
                (202, None),
                (200, [{"week": 1762560000, "days": [1, 0, 0, 0, 0, 0, 0]}]),
            ],
        )
        result = up.fetch_commit_activity(client, base)
        self.assertEqual(len(result), 1)
        self.assertEqual(client.sleeps, [2.0])

    def test_stats_202_exhaustion_returns_empty(self) -> None:
        base = self.base_path()
        client = FakeRepoClient(
            {}, stats_sequence=[(202, None), (202, None), (202, None)]
        )
        self.assertEqual(up.fetch_commit_activity(client, base), [])
        self.assertEqual(len(client.request_calls), 3)


class RendererTests(unittest.TestCase):
    def test_featured_empty(self) -> None:
        markdown, svg = up.render_featured([], {})
        self.assertIn("No public repositories", markdown)
        self.assertIsNone(svg)

    def test_featured_uses_curated_summary_and_signals(self) -> None:
        repo = make_repo(
            name="engine",
            tree_paths=["README.md", "tests/test_x.py", ".github/workflows/ci.yml", "Dockerfile"],
            commits=[make_commit()],
        )
        markdown, svg = up.render_featured([repo], {"engine": "Curated summary."})
        assert svg is not None
        self.assertIn("Curated summary.", svg)
        self.assertIn("tests", svg)
        self.assertIn("CI", svg)
        self.assertIn("Docker", svg)
        self.assertIn("Latest:", svg)
        self.assertIn("Ranking:", markdown)
        self.assertIn("Curated summary.", markdown)
        self.assertIn("[engine]", markdown)
        self.assertIn("assets/featured-projects.svg?v=", markdown)

    def test_featured_svg_escapes_untrusted_text(self) -> None:
        repo = make_repo(
            name="hostile",
            description="<script>alert(1)</script> <!-- hidden -->",
        )
        markdown, svg = up.render_featured([repo], {})
        assert svg is not None
        self.assertNotIn("<script>", svg)
        self.assertNotIn("<!--", svg)
        self.assertIn("&lt;script&gt;", svg)
        self.assertNotIn("<script>", markdown)

    def test_featured_svg_has_dark_card_surface(self) -> None:
        repo = make_repo(name="engine", description="Summary.")
        _markdown, svg = up.render_featured([repo], {})
        assert svg is not None
        self.assertIn(f'fill="{up.SVG_BG}"', svg)
        self.assertIn(f'fill="{up.SVG_PANEL}"', svg)
        self.assertIn("SELECTED ENGINEERING WORK", svg)

    def test_wrap_text_limits_lines_and_adds_ellipsis(self) -> None:
        lines = up.wrap_text("one two three four five six seven eight", 10, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("..."))

    def test_wrap_text_keeps_short_text_intact(self) -> None:
        self.assertEqual(up.wrap_text("short summary", 40, 3), ["short summary"])

    def test_project_index_excludes_featured(self) -> None:
        featured = make_repo(name="featured")
        other = make_repo(name="other")
        result = up.render_project_index([featured, other], [featured], {})
        self.assertIn("other", result)
        self.assertNotIn("featured", result)

    def test_project_index_escapes_pipe_in_table(self) -> None:
        repo = make_repo(name="other", description="one | two")
        result = up.render_project_index([repo], [], {})
        self.assertIn(r"one \| two", result)
        self.assertNotIn("one | two", result)

    def test_project_index_empty(self) -> None:
        self.assertIn("No additional", up.render_project_index([], [], {}))

    def test_activity_sorted_and_limited(self) -> None:
        repo = make_repo(
            commits=[
                make_commit(message="old", when=NOW - timedelta(days=10)),
                make_commit(message="new", when=NOW - timedelta(days=1)),
            ]
        )
        result = up.render_recent_activity([repo], limit=1)
        self.assertIn("new", result)
        self.assertNotIn("old", result)

    def test_activity_empty(self) -> None:
        self.assertIn("No public commits", up.render_recent_activity([]))

    def test_activity_escapes_message(self) -> None:
        repo = make_repo(commits=[make_commit(message="a | b <c> *d*")])
        result = up.render_recent_activity([repo])
        self.assertIn(r"a \| b &lt;c&gt; \*d\*", result)

    def test_releases_empty_is_honest(self) -> None:
        result = up.render_releases([])
        self.assertIn("No published releases", result)

    def test_releases_sorted(self) -> None:
        repo = make_repo(
            releases=[
                make_release(tag="v1", when=NOW - timedelta(days=20)),
                make_release(tag="v2", when=NOW - timedelta(days=1)),
            ]
        )
        result = up.render_releases([repo])
        self.assertLess(result.index("v2"), result.index("v1"))

    def test_release_prerelease_label(self) -> None:
        repo = make_repo(releases=[make_release(prerelease=True)])
        self.assertIn("pre-release", up.render_releases([repo]))

    def test_signals_empty(self) -> None:
        markdown, svg = up.render_repository_signals([])
        self.assertIn("unavailable", markdown)
        self.assertIsNone(svg)

    def test_signals_real_counts(self) -> None:
        one = make_repo(
            name="one",
            tree_paths=["tests/test_x.py", ".github/workflows/ci.yml"],
            languages={"Python": 100},
            commits=[make_commit()],
            releases=[make_release()],
        )
        two = make_repo(name="two", language="JavaScript", languages={"JavaScript": 50})
        markdown, svg = up.render_repository_signals([one, two])
        assert svg is not None
        self.assertIn("Public repositories", markdown)
        self.assertIn("**2**", markdown)
        self.assertIn("Python ×1", markdown)
        self.assertIn("Test suites detected", markdown)
        self.assertIn("**1** of 2", markdown)
        self.assertIn("2026-11-20", markdown)
        self.assertIn("PUBLIC REPOSITORIES", svg)
        self.assertIn("1/2", svg)
        self.assertIn("LANGUAGE SHARE BY BYTES", svg)
        self.assertIn("67%", svg)
        self.assertIn("2026-11-20", svg)
        self.assertIn("assets/repository-signals.svg?v=", markdown)

    def test_signals_note_incomplete_data(self) -> None:
        repo = make_repo(errors=["releases: boom"])
        markdown, _svg = up.render_repository_signals([repo])
        self.assertIn("could not be read", markdown)

    def test_aggregate_commit_activity(self) -> None:
        today = NOW.date()
        sunday = today - timedelta(days=(today.weekday() + 1) % 7)
        week_timestamp = int(
            datetime.combine(sunday, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        )
        repo = make_repo(
            activity_weeks=[
                {"week": week_timestamp, "days": [1, 2, 0, 0, 0, 0, 0]}
            ]
        )
        start, end, grid = up.aggregate_commit_activity([repo], NOW, weeks=12)
        self.assertEqual(start, sunday - timedelta(weeks=11))
        self.assertEqual(end, sunday)
        self.assertEqual(len(grid), 7)
        self.assertEqual(len(grid[0]), 12)
        self.assertEqual(sum(sum(row) for row in grid), 3)
        self.assertEqual(grid[0][-1], 1)
        self.assertEqual(grid[1][-1], 2)

    def test_commit_activity_without_data(self) -> None:
        markdown, svg = up.render_commit_activity([make_repo()], NOW, "assets/x.svg")
        self.assertIn("unavailable", markdown)
        self.assertIsNone(svg)

    def test_commit_activity_falls_back_to_commit_history(self) -> None:
        repo = make_repo(
            commits=[make_commit(when=NOW - timedelta(days=1))],
            activity_weeks=[],
        )
        markdown, svg = up.render_commit_activity([repo], NOW, "assets/x.svg")
        self.assertIsNotNone(svg)
        self.assertIn("recent commit history", markdown)
        self.assertIn("1 commit", markdown)

    def test_aggregate_uses_commit_fallback(self) -> None:
        repo = make_repo(
            commits=[make_commit(when=NOW)],
            activity_weeks=[],
        )
        _start, _end, grid = up.aggregate_commit_activity([repo], NOW, weeks=12)
        self.assertEqual(sum(sum(row) for row in grid), 1)

    def test_commit_activity_svg_and_cache_buster(self) -> None:
        today = NOW.date()
        sunday = today - timedelta(days=(today.weekday() + 1) % 7)
        week_timestamp = int(
            datetime.combine(sunday, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        )
        repo = make_repo(
            activity_weeks=[{"week": week_timestamp, "days": [0, 1, 0, 0, 0, 0, 0]}]
        )
        markdown, svg = up.render_commit_activity([repo], NOW, "assets/x.svg")
        self.assertIsNotNone(svg)
        assert svg is not None
        self.assertIn("assets/x.svg?v=", markdown)
        self.assertIn("<svg", svg)
        self.assertIn("<title>", svg)
        self.assertIn("Mon", svg)
        self.assertIn("1 commit", svg)
        self.assertIn("2026-11-21", markdown)
        self.assertNotIn("2026-11-28", markdown)
        start, _end, grid = up.aggregate_commit_activity([repo], NOW, 12)
        self.assertEqual(svg, up.render_activity_svg(start, grid, NOW.date()))


class SummaryTests(unittest.TestCase):
    def test_loads_valid_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summaries.json"
            path.write_text(json.dumps({"repo": "Summary."}), encoding="utf-8")
            self.assertEqual(up.load_project_summaries(path), {"repo": "Summary."})

    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            self.assertEqual(up.load_project_summaries(path), {})

    def test_malformed_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(up.load_project_summaries(path), {})

    def test_non_object_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "list.json"
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(up.load_project_summaries(path), {})


class FakeResponse:
    def __init__(
        self, status: int = 200, payload: object = None, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = json.dumps(payload).encode("utf-8") if payload is not None else b""

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class ClientTests(unittest.TestCase):
    def test_successful_request_and_authorization_header(self) -> None:
        captured = {}

        def opener(request, timeout):
            captured["auth"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return FakeResponse(payload=[{"ok": True}])

        client = up.GitHubClient(token="secret", opener=opener, sleep=lambda _: None)
        self.assertEqual(client.get("/x"), [{"ok": True}])
        self.assertEqual(captured["auth"], "Bearer secret")
        self.assertEqual(captured["timeout"], up.REQUEST_TIMEOUT)

    def test_no_authorization_without_token(self) -> None:
        captured = {}

        def opener(request, timeout):
            captured["auth"] = request.get_header("Authorization")
            return FakeResponse(payload={})

        up.GitHubClient(opener=opener, sleep=lambda _: None).get("/x")
        self.assertIsNone(captured["auth"])

    def test_retries_server_error_then_succeeds(self) -> None:
        attempts = {"count": 0}
        sleeps: list[float] = []

        def opener(request, timeout):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    500,
                    "boom",
                    {"X-RateLimit-Remaining": "10"},
                    io.BytesIO(b'{"message":"boom"}'),
                )
            return FakeResponse(payload={"ok": True})

        client = up.GitHubClient(opener=opener, sleep=sleeps.append)
        self.assertEqual(client.get("/x"), {"ok": True})
        self.assertEqual(attempts["count"], 2)
        self.assertEqual(sleeps, [1.0])

    def test_404_allow_missing(self) -> None:
        def opener(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 404, "not found", {}, io.BytesIO(b"{}")
            )

        client = up.GitHubClient(opener=opener, sleep=lambda _: None)
        self.assertEqual(client.request("/x", allow_missing=True), (404, None))

    def test_retry_after_is_honored(self) -> None:
        self.assertEqual(
            up.GitHubClient._backoff_delay(0, {"Retry-After": "7"}), 7.0
        )

    def test_scoped_token_404_falls_back_to_public_read(self) -> None:
        headers_seen: list[str | None] = []

        def opener(request, timeout):
            auth = request.get_header("Authorization")
            headers_seen.append(auth)
            if auth:
                raise urllib.error.HTTPError(
                    request.full_url,
                    404,
                    "not found for installation",
                    {"X-RateLimit-Remaining": "4999"},
                    io.BytesIO(b'{"message":"Not Found"}'),
                )
            return FakeResponse(payload={"public": True})

        client = up.GitHubClient(token="scoped", opener=opener, sleep=lambda _: None)
        self.assertEqual(client.get("/repos/owner/other"), {"public": True})
        self.assertEqual(headers_seen, ["Bearer scoped", None])

    def test_allow_missing_still_falls_back_to_public_read(self) -> None:
        headers_seen: list[str | None] = []

        def opener(request, timeout):
            auth = request.get_header("Authorization")
            headers_seen.append(auth)
            if auth:
                raise urllib.error.HTTPError(
                    request.full_url,
                    404,
                    "not found for installation",
                    {"X-RateLimit-Remaining": "4999"},
                    io.BytesIO(b"{}"),
                )
            return FakeResponse(payload={"ok": True})

        client = up.GitHubClient(token="scoped", opener=opener, sleep=lambda _: None)
        self.assertEqual(client.get("/repos/owner/other/languages", allow_missing=True), {"ok": True})
        self.assertEqual(headers_seen, ["Bearer scoped", None])


class AssemblyTests(unittest.TestCase):
    def readme_fixture(self) -> str:
        sections = "\n".join(
            f"<!-- {name}:start -->\nold\n<!-- {name}:end -->"
            for name in up.SECTION_NAMES
        )
        return f"# Manual title\n\n{sections}\n\nManual footer"

    def test_assembly_preserves_manual_text_and_updates_all_sections(self) -> None:
        updated, assets, _ranked = up.update_readme_content(
            self.readme_fixture(), [], NOW, {}, "assets"
        )
        self.assertIn("# Manual title", updated)
        self.assertIn("Manual footer", updated)
        self.assertNotIn("\nold\n", updated)
        self.assertEqual(assets, {})
        for name in up.SECTION_NAMES:
            self.assertEqual(updated.count(f"<!-- {name}:start -->"), 1)
            self.assertEqual(updated.count(f"<!-- {name}:end -->"), 1)

    def test_assembly_missing_section_raises(self) -> None:
        readme = self.readme_fixture().replace("<!-- latest-releases:start -->", "")
        with self.assertRaises(up.SectionError):
            up.update_readme_content(readme, [], NOW, {}, "assets")

    def test_assembly_empty_repository_list_uses_fallbacks(self) -> None:
        updated, assets, ranked = up.update_readme_content(
            self.readme_fixture(), [], NOW, {}, "assets"
        )
        self.assertEqual(ranked, [])
        self.assertEqual(assets, {})
        self.assertIn("No public repositories", updated)
        self.assertIn("No published releases", updated)
        self.assertIn("No public commits", updated)

    def test_assembly_emits_three_designed_assets(self) -> None:
        repo = make_repo(
            name="engine",
            description="A verified sample repository.",
            tree_paths=["README.md", "tests/test_x.py", "Dockerfile"],
            commits=[make_commit()],
        )
        updated, assets, _ranked = up.update_readme_content(
            self.readme_fixture(), [repo], NOW, {}, "assets"
        )
        self.assertEqual(
            set(assets),
            {
                "assets/featured-projects.svg",
                "assets/repository-signals.svg",
                "assets/commit-activity.svg",
            },
        )
        self.assertIn("assets/featured-projects.svg?v=", updated)
        self.assertIn("assets/repository-signals.svg?v=", updated)
        self.assertIn("assets/commit-activity.svg?v=", updated)

    def test_assembly_is_idempotent(self) -> None:
        first, _assets, _ranked = up.update_readme_content(
            self.readme_fixture(), [], NOW, {}, "assets"
        )
        second, _assets, _ranked = up.update_readme_content(
            first, [], NOW, {}, "assets"
        )
        self.assertEqual(first, second)


class FakeMainClient:
    """Fake GitHub client returning one fully-populated public repository."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token
        today = datetime.now(timezone.utc).date()
        sunday = today - timedelta(days=(today.weekday() + 1) % 7)
        self.week = int(
            datetime.combine(sunday, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        )

    def get(self, path: str, *, allow_missing: bool = False) -> object:
        if path.startswith("/users/"):
            return [
                {
                    "name": "sample-repo",
                    "owner": {"login": "owner"},
                    "description": "A verified sample repository.",
                    "html_url": "https://github.com/owner/sample-repo",
                    "language": "Python",
                    "stargazers_count": 0,
                    "forks_count": 0,
                    "updated_at": "2026-11-13T06:31:06Z",
                    "pushed_at": "2026-11-13T06:31:06Z",
                    "default_branch": "main",
                    "topics": [],
                    "open_issues_count": 0,
                }
            ]
        if path.endswith("/languages"):
            return {"Python": 500}
        if "/releases?" in path:
            return []
        if "/commits?" in path:
            return [
                {
                    "sha": "a" * 40,
                    "html_url": "https://github.com/owner/sample-repo/commit/" + "a" * 40,
                    "commit": {
                        "message": "feat: sample commit",
                        "committer": {"date": "2026-11-13T06:31:06Z"},
                    },
                }
            ]
        if "/git/trees/" in path:
            return {
                "tree": [
                    {"path": "README.md", "type": "blob"},
                    {"path": "Dockerfile", "type": "blob"},
                    {"path": "tests/test_sample.py", "type": "blob"},
                    {"path": ".github/workflows/ci.yml", "type": "blob"},
                ],
                "truncated": False,
            }
        return None

    def request(self, path: str, *, allow_missing: bool = False) -> tuple[int, object]:
        return 200, [{"week": self.week, "days": [0, 1, 0, 0, 0, 0, 0]}]

    def sleep(self, seconds: float) -> None:
        return None


class FailingDiscoveryClient(FakeMainClient):
    def get(self, path: str, *, allow_missing: bool = False) -> object:
        raise up.GitHubError("discovery failed", status=500)


class MainFlowTests(unittest.TestCase):
    def readme_fixture(self) -> str:
        sections = "\n".join(
            f"<!-- {name}:start -->\nold\n<!-- {name}:end -->"
            for name in up.SECTION_NAMES
        )
        return f"# Manual title\n\n{sections}\n\nManual footer\n"

    def test_main_writes_real_content_and_check_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README.md"
            readme.write_text(self.readme_fixture(), encoding="utf-8")
            env = {
                "GITHUB_TOKEN": "",
                "GH_TOKEN": "",
                "GITHUB_REPOSITORY_OWNER": "owner",
                "GITHUB_REPOSITORY": "owner/profile",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                up, "GitHubClient", FakeMainClient
            ):
                self.assertEqual(up.main(["--readme", str(readme)]), 0)
                content = readme.read_text(encoding="utf-8")
                self.assertIn("# Manual title", content)
                self.assertIn("[sample-repo](https://github.com/owner/sample-repo)", content)
                self.assertIn("assets/featured-projects.svg?v=", content)
                self.assertIn("assets/repository-signals.svg?v=", content)
                self.assertIn("assets/commit-activity.svg?v=", content)
                self.assertIn("feat: sample commit", content)
                self.assertIn("No published releases", content)
                self.assertNotIn("_Generated by the profile updater._", content)
                featured_svg = Path(directory) / "assets" / "featured-projects.svg"
                signals_svg = Path(directory) / "assets" / "repository-signals.svg"
                activity_svg = Path(directory) / "assets" / "commit-activity.svg"
                self.assertTrue(featured_svg.exists())
                self.assertTrue(signals_svg.exists())
                self.assertTrue(activity_svg.exists())
                self.assertIn(
                    "A verified sample repository.",
                    featured_svg.read_text(encoding="utf-8"),
                )
                self.assertEqual(up.main(["--check", "--readme", str(readme)]), 0)

    def test_check_detects_stale_generated_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README.md"
            readme.write_text(self.readme_fixture(), encoding="utf-8")
            env = {
                "GITHUB_TOKEN": "",
                "GH_TOKEN": "",
                "GITHUB_REPOSITORY_OWNER": "owner",
                "GITHUB_REPOSITORY": "owner/profile",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                up, "GitHubClient", FakeMainClient
            ):
                self.assertEqual(up.main(["--readme", str(readme)]), 0)
                text = readme.read_text(encoding="utf-8")
                text = text.replace(
                    "No published releases across public repositories yet.",
                    "stale manual edit",
                    1,
                )
                readme.write_text(text, encoding="utf-8")
                self.assertEqual(up.main(["--check", "--readme", str(readme)]), 1)

    def test_check_detects_stale_generated_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README.md"
            readme.write_text(self.readme_fixture(), encoding="utf-8")
            env = {
                "GITHUB_TOKEN": "",
                "GH_TOKEN": "",
                "GITHUB_REPOSITORY_OWNER": "owner",
                "GITHUB_REPOSITORY": "owner/profile",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                up, "GitHubClient", FakeMainClient
            ):
                self.assertEqual(up.main(["--readme", str(readme)]), 0)
                asset = Path(directory) / "assets" / "repository-signals.svg"
                asset.write_text("stale asset", encoding="utf-8")
                self.assertEqual(up.main(["--check", "--readme", str(readme)]), 1)

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README.md"
            readme.write_text(self.readme_fixture(), encoding="utf-8")
            before = readme.read_text(encoding="utf-8")
            env = {
                "GITHUB_TOKEN": "",
                "GH_TOKEN": "",
                "GITHUB_REPOSITORY_OWNER": "owner",
                "GITHUB_REPOSITORY": "owner/profile",
            }
            output = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                up, "GitHubClient", FakeMainClient
            ), contextlib.redirect_stdout(output):
                self.assertEqual(up.main(["--dry-run", "--readme", str(readme)]), 0)
            self.assertEqual(readme.read_text(encoding="utf-8"), before)
            self.assertIn("Featured engineering projects: sample-repo", output.getvalue())
            self.assertFalse((Path(directory) / "assets").exists())

    def test_missing_readme_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.md"
            self.assertEqual(up.main(["--readme", str(missing)]), 1)
            self.assertFalse(missing.exists())

    def test_discovery_failure_leaves_readme_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README.md"
            original = self.readme_fixture()
            readme.write_text(original, encoding="utf-8")
            env = {
                "GITHUB_TOKEN": "",
                "GH_TOKEN": "",
                "GITHUB_REPOSITORY_OWNER": "owner",
                "GITHUB_REPOSITORY": "owner/profile",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                up, "GitHubClient", FailingDiscoveryClient
            ):
                self.assertEqual(up.main(["--readme", str(readme)]), 1)
            self.assertEqual(readme.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
