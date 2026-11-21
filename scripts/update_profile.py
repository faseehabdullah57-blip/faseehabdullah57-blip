#!/usr/bin/env python3
"""Generate the automated sections of the GitHub profile README.

The updater discovers the owner's public, non-fork repositories through the
GitHub REST API, reads factual repository signals, ranks the projects with a
transparent score, and rewrites only the delimited sections of README.md.

Design constraints:

* Python standard library only at runtime.
* A failure in one repository must never abort the run.
* Generated Markdown must be deterministic so the workflow commits only when
  real data changes.
* Repository descriptions and commit messages are untrusted input and are
  escaped before they reach the README.
* The updater never invents projects, releases, metrics, or achievements.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

DEFAULT_OWNER = "faseehabdullah57-blip"
DEFAULT_PROFILE_REPO = "faseehabdullah57-blip"
API_ROOT = "https://api.github.com"
GITHUB_HOST = "https://github.com"

PER_PAGE = 100
MAX_REPO_PAGES = 3
MAX_REPOS = 300
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
RETRY_BASE_SECONDS = 1.0
MAX_RETRY_SLEEP_SECONDS = 20.0
MAX_RATE_LIMIT_SLEEP_SECONDS = 15.0

FEATURED_LIMIT = 4
ACTIVITY_LIMIT = 6
RELEASE_LIMIT = 5
HEATMAP_WEEKS = 12

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

SECTION_NAMES = (
    "featured-projects",
    "project-index",
    "recent-activity",
    "latest-releases",
    "repository-signals",
    "commit-activity",
)

SUMMARY_PATH = Path(__file__).resolve().parent / "project_summaries.json"

log = logging.getLogger("profile-updater")


class GitHubError(RuntimeError):
    """A GitHub API request failed after retries."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SectionError(RuntimeError):
    """A README section marker is missing, duplicated, or out of order."""


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class GitHubClient:
    """Small GitHub REST client with retries and rate-limit awareness."""

    def __init__(
        self,
        token: str | None = None,
        api_root: str = API_ROOT,
        timeout: int = REQUEST_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.token = token or None
        self.api_root = api_root.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep
        self.opener = opener

    def _headers(self, authenticated: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "profile-readme-updater",
        }
        if authenticated and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(self, path: str, *, allow_missing: bool = False) -> tuple[int, Any]:
        """Return ``(status, parsed_json)`` for a GET request.

        ``allow_missing`` treats 404/409 as ``(status, None)`` instead of an
        error. Empty repositories return 409 from some endpoints.
        """
        return self._request(path, allow_missing=allow_missing, authenticated=True)

    def _request(
        self,
        path: str,
        *,
        allow_missing: bool,
        authenticated: bool,
    ) -> tuple[int, Any]:
        url = f"{self.api_root}{path}"
        last_error: GitHubError | None = None

        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(url, headers=self._headers(authenticated))
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    status = int(getattr(response, "status", 0) or response.getcode())
                    headers = getattr(response, "headers", None) or {}
                    self._log_rate_limit(headers)
                    raw = response.read()
                    data = json.loads(raw.decode("utf-8")) if raw else None
                    if allow_missing and status in (404, 409):
                        return status, None
                    return status, data

            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                headers = exc.headers or {}
                self._log_rate_limit(headers)
                detail = _safe_read_error(exc)
                last_error = GitHubError(
                    f"GitHub API HTTP {status} for {path}: {detail}", status=status
                )

                remaining = str(headers.get("X-RateLimit-Remaining", ""))
                if (
                    authenticated
                    and self.token
                    and status in (403, 404)
                    and not (status == 403 and remaining == "0")
                ):
                    # A scoped workflow GITHUB_TOKEN can 404 on public repos it
                    # was not installed on. Public reads must still work, so
                    # retry once without credentials.
                    log.info(
                        "Authenticated request returned HTTP %d for %s; "
                        "retrying without credentials for public data.",
                        status,
                        path,
                    )
                    return self._request(
                        path, allow_missing=allow_missing, authenticated=False
                    )

                if allow_missing and status in (404, 409):
                    return status, None

                if (
                    status == 403
                    and str(headers.get("X-RateLimit-Remaining", "")) == "0"
                    and attempt < self.max_retries
                ):
                    delay = self._rate_limit_delay(headers)
                    log.warning(
                        "GitHub API rate limit exhausted; sleeping %.0fs before retry.",
                        delay,
                    )
                    self.sleep(delay)
                    continue

                if status in RETRYABLE_STATUSES and attempt < self.max_retries:
                    delay = self._backoff_delay(attempt, headers)
                    log.warning(
                        "GitHub API returned HTTP %d; retrying in %.1fs.", status, delay
                    )
                    self.sleep(delay)
                    continue

                raise last_error from exc

            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = GitHubError(f"Network error for {path}: {exc}")
                if attempt < self.max_retries:
                    delay = self._backoff_delay(attempt, {})
                    log.warning(
                        "Network error for %s; retrying in %.1fs.", path, delay
                    )
                    self.sleep(delay)
                    continue
                raise last_error from exc

            except json.JSONDecodeError as exc:
                raise GitHubError(f"Invalid JSON returned for {path}: {exc}") from exc

        raise last_error or GitHubError(f"Request failed: {path}")

    def get(self, path: str, *, allow_missing: bool = False) -> Any:
        _status, data = self.request(path, allow_missing=allow_missing)
        return data

    @staticmethod
    def _log_rate_limit(headers: Any) -> None:
        remaining = headers.get("X-RateLimit-Remaining") if headers else None
        if remaining is None:
            return
        try:
            value = int(remaining)
        except (TypeError, ValueError):
            return
        if value <= 5:
            log.warning("GitHub API rate limit: %d request(s) remaining.", value)
        else:
            log.debug("GitHub API rate limit: %d request(s) remaining.", value)

    @staticmethod
    def _backoff_delay(attempt: int, headers: Any) -> float:
        retry_after = headers.get("Retry-After") if headers else None
        if retry_after:
            try:
                return max(1.0, min(float(retry_after), MAX_RETRY_SLEEP_SECONDS))
            except (TypeError, ValueError):
                pass
        return min(RETRY_BASE_SECONDS * (2**attempt), MAX_RETRY_SLEEP_SECONDS)

    @staticmethod
    def _rate_limit_delay(headers: Any) -> float:
        reset = headers.get("X-RateLimit-Reset") if headers else None
        if reset:
            try:
                delay = float(reset) - time.time()
                return max(1.0, min(delay, MAX_RATE_LIMIT_SLEEP_SECONDS))
            except (TypeError, ValueError):
                pass
        return 5.0


def _safe_read_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:300]
    except Exception:  # pragma: no cover - defensive around HTTPError streams
        return ""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Commit:
    repo: str
    repo_url: str
    sha: str
    short_sha: str
    message: str
    date: datetime
    url: str


@dataclass(frozen=True)
class Release:
    repo: str
    tag: str
    name: str
    published: datetime
    url: str
    prerelease: bool = False


@dataclass
class TreeResult:
    paths: list[str] = field(default_factory=list)
    truncated: bool = False


@dataclass
class Repo:
    name: str
    owner: str
    description: str | None
    url: str
    language: str | None
    stars: int
    forks: int
    updated_at: str | None
    pushed_at: str | None
    default_branch: str = "main"
    archived: bool = False
    topics: list[str] = field(default_factory=list)
    open_issues: int = 0
    languages: dict[str, int] = field(default_factory=dict)
    releases: list[Release] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)
    activity_weeks: list[dict[str, Any]] = field(default_factory=list)
    tree_paths: list[str] = field(default_factory=list)
    tree_truncated: bool = False
    errors: list[str] = field(default_factory=list)
    score: float = 0.0

    @classmethod
    def from_api(cls, data: dict[str, Any], owner: str) -> "Repo":
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("repository payload has no name")
        repo_owner = str((data.get("owner") or {}).get("login") or owner)
        fallback_url = (
            f"{GITHUB_HOST}/{urllib.parse.quote(repo_owner, safe='')}/"
            f"{urllib.parse.quote(name, safe='')}"
        )
        return cls(
            name=name,
            owner=repo_owner,
            description=_clean_optional(data.get("description")),
            url=_safe_url(data.get("html_url"), fallback_url),
            language=_clean_optional(data.get("language")),
            stars=_as_int(data.get("stargazers_count")),
            forks=_as_int(data.get("forks_count")),
            updated_at=_clean_optional(data.get("updated_at")),
            pushed_at=_clean_optional(data.get("pushed_at"))
            or _clean_optional(data.get("updated_at")),
            default_branch=str(data.get("default_branch") or "main"),
            archived=bool(data.get("archived")),
            topics=[str(item) for item in data.get("topics") or []],
            open_issues=_as_int(data.get("open_issues_count")),
        )

    @property
    def has_tests(self) -> bool:
        for path in self.tree_paths:
            parts = path.lower().split("/")
            if any(part in {"tests", "test", "__tests__"} for part in parts[:-1]):
                return True
            filename = parts[-1]
            if filename.startswith("test_") and filename.endswith(".py"):
                return True
            if filename.endswith("_test.py"):
                return True
            if ".test." in filename or ".spec." in filename:
                return True
        return False

    @property
    def has_ci(self) -> bool:
        return any(
            path.lower().startswith(".github/workflows/")
            and path.lower().endswith((".yml", ".yaml"))
            for path in self.tree_paths
        )

    @property
    def has_docker(self) -> bool:
        docker_files = {"dockerfile", "docker-compose.yml", "docker-compose.yaml"}
        return any(path.lower().rsplit("/", 1)[-1] in docker_files for path in self.tree_paths)

    @property
    def has_readme(self) -> bool:
        return any(
            "/" not in path
            and path.lower() in {"readme.md", "readme.rst", "readme.txt", "readme"}
            for path in self.tree_paths
        )

    @property
    def file_count(self) -> int:
        return len(self.tree_paths)

    @property
    def latest_commit(self) -> Commit | None:
        return max(self.commits, key=lambda item: item.date) if self.commits else None

    @property
    def latest_release(self) -> Release | None:
        return (
            max(self.releases, key=lambda item: item.published)
            if self.releases
            else None
        )


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _clean_optional(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _safe_url(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.startswith(f"{GITHUB_HOST}/"):
        return value
    return fallback


def parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_date(value: Any) -> str:
    parsed = parse_iso(value)
    return parsed.date().isoformat() if parsed else "unknown"


# ---------------------------------------------------------------------------
# Discovery and collection
# ---------------------------------------------------------------------------


def repo_api_path(owner: str, name: str) -> str:
    return (
        f"/repos/{urllib.parse.quote(owner, safe='')}/"
        f"{urllib.parse.quote(name, safe='')}"
    )


def discover_public_repositories(
    client: GitHubClient, owner: str, profile_repo: str
) -> list[dict[str, Any]]:
    """Return public, non-fork repositories owned by ``owner``.

    Always uses the public ``/users/{owner}/repos`` endpoint. A workflow
    ``GITHUB_TOKEN`` is scoped to the profile repository, so ``/user/repos``
    would not reliably list the owner's other repositories. The token is still
    sent when present to raise the rate limit; the client falls back to an
    unauthenticated read if the scoped token cannot see a public repository.
    """
    discovered: list[dict[str, Any]] = []
    seen: set[str] = set()
    profile_key = profile_repo.lower()

    for page in range(1, MAX_REPO_PAGES + 1):
        path = (
            f"/users/{urllib.parse.quote(owner, safe='')}/repos"
            f"?type=owner&sort=updated&direction=desc"
            f"&per_page={PER_PAGE}&page={page}"
        )

        data = client.get(path)
        if data is None:
            break
        if not isinstance(data, list):
            raise GitHubError("Unexpected response while listing repositories.")

        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            key = name.lower()
            if not name or key in seen:
                continue
            if item.get("private") or item.get("fork"):
                continue
            if key == profile_key:
                continue
            seen.add(key)
            discovered.append(item)
            if len(discovered) >= MAX_REPOS:
                break

        if len(data) < PER_PAGE or len(discovered) >= MAX_REPOS:
            break

    discovered.sort(
        key=lambda item: parse_iso(item.get("updated_at")) or datetime.min.replace(
            tzinfo=timezone.utc
        ),
        reverse=True,
    )
    return discovered


def fetch_languages(client: GitHubClient, base: str) -> dict[str, int]:
    data = client.get(f"{base}/languages", allow_missing=True)
    if not isinstance(data, dict):
        return {}
    return {
        str(name): _as_int(count)
        for name, count in data.items()
        if isinstance(name, str)
    }


def fetch_releases(client: GitHubClient, base: str, repo: Repo) -> list[Release]:
    data = client.get(f"{base}/releases?per_page=10", allow_missing=True)
    if not isinstance(data, list):
        return []

    releases: list[Release] = []
    for item in data:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        published = parse_iso(item.get("published_at"))
        if published is None:
            continue
        tag = str(item.get("tag_name") or "").strip()
        name = str(item.get("name") or tag or "release").strip()
        fallback = (
            f"{repo.url}/releases/tag/{urllib.parse.quote(tag, safe='')}"
            if tag
            else repo.url
        )
        releases.append(
            Release(
                repo=repo.name,
                tag=tag or "untagged",
                name=name,
                published=published,
                url=_safe_url(item.get("html_url"), fallback),
                prerelease=bool(item.get("prerelease")),
            )
        )
    releases.sort(key=lambda item: item.published, reverse=True)
    return releases


def fetch_commits(client: GitHubClient, base: str, repo: Repo) -> list[Commit]:
    data = client.get(f"{base}/commits?per_page=10", allow_missing=True)
    if not isinstance(data, list):
        return []

    commits: list[Commit] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        sha = str(item.get("sha") or "").strip()
        commit_data = item.get("commit") or {}
        if not sha or not isinstance(commit_data, dict):
            continue
        message = str(commit_data.get("message") or "").splitlines()
        subject = message[0] if message else ""
        committer = commit_data.get("committer") or {}
        author = commit_data.get("author") or {}
        committed_at = parse_iso(
            (committer.get("date") if isinstance(committer, dict) else None)
            or (author.get("date") if isinstance(author, dict) else None)
        )
        if committed_at is None:
            continue
        commits.append(
            Commit(
                repo=repo.name,
                repo_url=repo.url,
                sha=sha,
                short_sha=sha[:7],
                message=subject,
                date=committed_at,
                url=_safe_url(item.get("html_url"), f"{repo.url}/commit/{sha}"),
            )
        )
    commits.sort(key=lambda item: item.date, reverse=True)
    return commits


def fetch_tree(client: GitHubClient, base: str, branch: str) -> TreeResult:
    data = client.get(
        f"{base}/git/trees/{urllib.parse.quote(branch, safe='')}?recursive=1",
        allow_missing=True,
    )
    if not isinstance(data, dict):
        return TreeResult()
    paths = [
        str(item["path"])
        for item in data.get("tree") or []
        if isinstance(item, dict) and item.get("type") == "blob" and item.get("path")
    ]
    return TreeResult(paths=paths, truncated=bool(data.get("truncated")))


def fetch_commit_activity(client: GitHubClient, base: str) -> list[dict[str, Any]]:
    """Return weekly commit activity, waiting briefly for GitHub to compute it.

    GitHub returns 202 while the statistics are being generated. A 404/204 means
    the repository has no commit history.
    """
    for attempt in range(3):
        status, data = client.request(
            f"{base}/stats/commit_activity", allow_missing=True
        )
        if status == 200:
            if not isinstance(data, list):
                return []
            weeks: list[dict[str, Any]] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                week = item.get("week")
                days = item.get("days")
                if isinstance(week, int) and isinstance(days, list) and len(days) == 7:
                    weeks.append(
                        {"week": week, "days": [_as_int(value) for value in days]}
                    )
            return weeks
        if status in (204, 404):
            return []
        if status == 202:
            if attempt < 2:
                client.sleep(2.0 * (attempt + 1))
                continue
            log.warning("Commit statistics are still being computed for %s.", base)
            return []
        raise GitHubError(
            f"Unexpected HTTP {status} from commit activity endpoint for {base}."
        )
    return []


def collect_repository(
    client: GitHubClient, owner: str, api_data: dict[str, Any]
) -> Repo:
    """Collect all facts for one repository, isolating endpoint failures."""
    repo = Repo.from_api(api_data, owner)
    base = repo_api_path(repo.owner, repo.name)

    def guarded(label: str, fetch: Callable[[], Any], default: Any) -> Any:
        try:
            return fetch()
        except GitHubError as exc:
            repo.errors.append(f"{label}: {exc}")
            log.warning("%s: could not read %s: %s", repo.name, label, exc)
            return default

    repo.languages = guarded("languages", lambda: fetch_languages(client, base), {})
    repo.releases = guarded(
        "releases", lambda: fetch_releases(client, base, repo), []
    )
    repo.commits = guarded("commits", lambda: fetch_commits(client, base, repo), [])

    tree = guarded("file tree", lambda: fetch_tree(client, base, repo.default_branch), TreeResult())
    repo.tree_paths = tree.paths
    repo.tree_truncated = tree.truncated

    repo.activity_weeks = guarded(
        "commit activity", lambda: fetch_commit_activity(client, base), []
    )
    return repo


# ---------------------------------------------------------------------------
# Transparent ranking
# ---------------------------------------------------------------------------


def repository_score(repo: Repo, now: datetime) -> float:
    """Score a repository from observable signals.

    Weights are intentionally explicit and capped so one large repository
    cannot dominate indefinitely:

    * recency of last push: up to 40
    * stars: up to 20 (0.4 each, capped at 50 stars)
    * forks: up to 10 (0.5 each, capped at 20 forks)
    * a description: 5
    * tests detected: 10
    * CI workflow detected: 8
    * Docker detected: 4
    * a release in the last year: 8
    * README at the repository root: 5
    * repository size: up to 10 (0.4 per file, capped at 25 files)
    """
    score = 0.0
    pushed = parse_iso(repo.pushed_at) or parse_iso(repo.updated_at)
    if pushed:
        days = max(0.0, (now - pushed).total_seconds() / 86400.0)
        score += 40.0 * max(0.0, 1.0 - days / 365.0)

    score += min(max(repo.stars, 0), 50) * 0.4
    score += min(max(repo.forks, 0), 20) * 0.5

    if repo.description:
        score += 5.0
    if repo.has_tests:
        score += 10.0
    if repo.has_ci:
        score += 8.0
    if repo.has_docker:
        score += 4.0

    release = repo.latest_release
    if release and (now - release.published).days <= 365:
        score += 8.0
    if repo.has_readme:
        score += 5.0

    score += min(repo.file_count, 25) * 0.4
    return round(score, 2)


def rank_repositories(repos: Sequence[Repo], now: datetime) -> list[Repo]:
    for repo in repos:
        repo.score = repository_score(repo, now)

    def pushed_timestamp(repo: Repo) -> float:
        parsed = parse_iso(repo.pushed_at)
        return parsed.timestamp() if parsed else 0.0

    return sorted(
        repos,
        key=lambda repo: (
            -repo.score,
            -repo.stars,
            -pushed_timestamp(repo),
            repo.name.lower(),
        ),
    )


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MARKDOWN_SPECIALS = re.compile(r"([\\`*_\[\]|])")


def sanitize_inline(value: Any, limit: int | None = 200) -> str:
    """Normalize untrusted text and remove section-marker syntax."""
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = text.replace("<!--", "").replace("-->", "")
    text = _CONTROL_CHARS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 3)].rstrip() + "..."
    return text


def escape_markdown(value: Any, limit: int | None = 200) -> str:
    """Escape text for safe inline Markdown and table-cell use."""
    text = sanitize_inline(value, limit)
    text = html.unescape(text)
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return _MARKDOWN_SPECIALS.sub(r"\\\1", text)


def repo_summary(repo: Repo, summaries: dict[str, str]) -> str:
    curated = summaries.get(repo.name)
    if curated:
        return curated
    if repo.description:
        return repo.description
    return "No repository description provided."


def load_project_summaries(path: Path = SUMMARY_PATH) -> dict[str, str]:
    """Load optional curated summaries; malformed files degrade to empty."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("Project summaries file not found at %s; using API text.", path)
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read project summaries from %s: %s", path, exc)
        return {}

    if not isinstance(raw, dict):
        log.warning("Project summaries must be a JSON object; ignoring %s.", path)
        return {}

    summaries: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str) and value.strip():
            summaries[key] = value.strip()
    return summaries


def _format_count(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


# ---------------------------------------------------------------------------
# Section renderers (pure functions, tested without network access)
# ---------------------------------------------------------------------------


def render_featured(
    featured: Sequence[Repo], summaries: dict[str, str], limit: int = FEATURED_LIMIT
) -> str:
    if not featured:
        return "_No public repositories discovered yet._"

    lines: list[str] = []
    for repo in list(featured)[:limit]:
        signals: list[str] = []
        if repo.language:
            signals.append(f"`{escape_markdown(repo.language, 40)}`")
        if repo.has_tests:
            signals.append("tests")
        if repo.has_ci:
            signals.append("CI")
        if repo.has_docker:
            signals.append("Docker")
        if repo.stars:
            signals.append(f"{repo.stars} star{'s' if repo.stars != 1 else ''}")
        updated = format_date(repo.pushed_at)
        if updated != "unknown":
            signals.append(f"updated {updated}")

        lines.append(f"### [{escape_markdown(repo.name, 80)}]({repo.url})")
        lines.append("")
        lines.append(escape_markdown(repo_summary(repo, summaries), 240))
        lines.append("")
        lines.append(" · ".join(signals) if signals else "_No repository signals available._")

        latest = repo.latest_commit
        if latest:
            lines.append("")
            lines.append(
                f"Latest: {escape_markdown(latest.message or 'No commit message', 120)} "
                f"(`{latest.short_sha}`)"
            )
        lines.append("")

    lines.append(
        "_Ranking: recency, stars/forks, description, tests, CI, Docker, releases, "
        "documentation, and repository size. Ties break by stars, then most recent push._"
    )
    return "\n".join(lines).strip()


def render_project_index(
    repos: Sequence[Repo],
    featured: Sequence[Repo],
    summaries: dict[str, str],
) -> str:
    featured_names = {repo.name for repo in featured}
    remaining = [repo for repo in repos if repo.name not in featured_names]
    if not remaining:
        return "_No additional public repositories._"

    lines = [
        "| Repository | Focus | Stack | Updated |",
        "| :--- | :--- | :--- | :--- |",
    ]
    for repo in remaining:
        focus = escape_markdown(repo_summary(repo, summaries), 150)
        stack = escape_markdown(repo.language or "Unspecified", 40)
        updated = format_date(repo.pushed_at)
        lines.append(
            f"| [{escape_markdown(repo.name, 80)}]({repo.url}) "
            f"| {focus} | {stack} | {updated} |"
        )
    return "\n".join(lines)


def render_recent_activity(
    repos: Sequence[Repo], limit: int = ACTIVITY_LIMIT
) -> str:
    commits = [commit for repo in repos for commit in repo.commits]
    if not commits:
        return "_No public commits available._"

    commits.sort(key=lambda item: (item.date, item.sha), reverse=True)
    lines: list[str] = []
    for commit in commits[:limit]:
        message = escape_markdown(commit.message or "No commit message", 130)
        lines.append(
            f"- **{format_date(commit.date)}** · "
            f"[{escape_markdown(commit.repo, 80)}]({commit.repo_url}) · "
            f"{message} ([`{commit.short_sha}`]({commit.url}))"
        )
    return "\n".join(lines)


def render_releases(repos: Sequence[Repo], limit: int = RELEASE_LIMIT) -> str:
    releases = [release for repo in repos for release in repo.releases]
    if not releases:
        return (
            "_No published releases across public repositories yet. "
            "Tagged releases will appear here automatically._"
        )

    releases.sort(key=lambda item: (item.published, item.tag), reverse=True)
    lines: list[str] = []
    for release in releases[:limit]:
        label = f"{release.repo} {release.tag}".strip()
        detail = ""
        if release.name and release.name != release.tag:
            detail = f" — {escape_markdown(release.name, 90)}"
        suffix = " (pre-release)" if release.prerelease else ""
        lines.append(
            f"- **{format_date(release.published)}** · "
            f"[{escape_markdown(label, 100)}]({release.url}){detail}{suffix}"
        )
    return "\n".join(lines)


def render_repository_signals(repos: Sequence[Repo]) -> str:
    if not repos:
        return "_Repository signals are unavailable._"

    count = len(repos)
    primary_languages = Counter(repo.language or "Unspecified" for repo in repos)
    language_text = " · ".join(
        f"{escape_markdown(language, 40)} ×{amount}"
        for language, amount in sorted(
            primary_languages.items(), key=lambda pair: (-pair[1], pair[0].lower())
        )
    )

    byte_totals: Counter[str] = Counter()
    for repo in repos:
        byte_totals.update(repo.languages)
    total_bytes = sum(byte_totals.values())
    if total_bytes:
        language_share = " · ".join(
            f"{escape_markdown(language, 40)} {amount / total_bytes:.0%}"
            for language, amount in byte_totals.most_common(3)
        )
    else:
        language_share = "_Unavailable_"

    tests = sum(1 for repo in repos if repo.has_tests)
    ci = sum(1 for repo in repos if repo.has_ci)
    with_releases = sum(1 for repo in repos if repo.releases)
    latest_commit = max(
        (repo.latest_commit for repo in repos if repo.latest_commit),
        key=lambda item: item.date,
        default=None,
    )
    latest_text = format_date(latest_commit.date) if latest_commit else "_Unavailable_"

    rows = [
        ("Public repositories", f"**{count}**"),
        ("Primary languages", language_text or "_Unavailable_"),
        ("Top 3 languages by bytes", language_share),
        ("Test suites detected", f"**{tests}** of {count}"),
        ("CI workflows detected", f"**{ci}** of {count}"),
        ("Repositories with releases", f"**{with_releases}**"),
        ("Latest public commit", f"**{latest_text}**"),
    ]
    body = "\n".join(
        ["| Signal | Value |", "| :--- | :--- |"]
        + [f"| {label} | {value} |" for label, value in rows]
    )

    unavailable = sum(1 for repo in repos if repo.errors)
    if unavailable:
        body += (
            f"\n\n_Data could not be read for "
            f"{_format_count(unavailable, 'repository', 'repositories')}; "
            "signals may be incomplete._"
        )
    body += (
        "\n\n_Excludes this profile repository. Signals are derived from default branches._"
    )
    return body


def activity_weeks_from_commits(commits: Sequence[Commit]) -> list[dict[str, Any]]:
    """Derive weekly activity buckets from commit timestamps.

    This is the fallback when GitHub's statistics endpoint is still computing.
    It is exact when the recent commit list is complete; for very busy
    repositories it reflects the commits returned by the API.
    """
    day_counts: Counter[date] = Counter()
    for commit in commits:
        day_counts[commit.date.astimezone(timezone.utc).date()] += 1

    buckets: dict[date, list[int]] = {}
    for day, count in day_counts.items():
        sunday = day - timedelta(days=(day.weekday() + 1) % 7)
        buckets.setdefault(sunday, [0] * 7)[(day.weekday() + 1) % 7] += count

    return [
        {
            "week": int(
                datetime.combine(
                    sunday, datetime.min.time(), tzinfo=timezone.utc
                ).timestamp()
            ),
            "days": days,
        }
        for sunday, days in sorted(buckets.items())
    ]


def aggregate_commit_activity(
    repos: Sequence[Repo], now: datetime, weeks: int = HEATMAP_WEEKS
) -> tuple[date, date, list[list[int]]]:
    """Aggregate weekly commit statistics into a Sunday-starting day grid.

    Falls back to the recent commit list when GitHub has not finished
    computing repository statistics.
    """
    day_counts: Counter[date] = Counter()
    for repo in repos:
        week_data = repo.activity_weeks or activity_weeks_from_commits(repo.commits)
        for week in week_data:
            try:
                week_start = datetime.fromtimestamp(
                    int(week["week"]), tz=timezone.utc
                ).date()
            except (KeyError, TypeError, ValueError, OSError):
                continue
            days = week.get("days") or []
            for offset, value in enumerate(days[:7]):
                day_counts[week_start + timedelta(days=offset)] += _as_int(value)

    today = now.astimezone(timezone.utc).date()
    week_end = today - timedelta(days=(today.weekday() + 1) % 7)
    week_start = week_end - timedelta(weeks=max(weeks, 1) - 1)

    grid = [
        [
            day_counts.get(week_start + timedelta(weeks=column, days=row), 0)
            for column in range(max(weeks, 1))
        ]
        for row in range(7)
    ]
    return week_start, week_end, grid


def _activity_color(count: int, maximum: int) -> str:
    if count <= 0:
        return "#ebedf0"
    if maximum <= 1:
        return "#40c463"
    scale = ("#9be9a8", "#40c463", "#30a14e", "#216e39")
    level = min(3, max(0, int((count / maximum) * 3)))
    return scale[level]


def render_activity_svg(start: date, grid: Sequence[Sequence[int]]) -> str:
    """Render a dependency-free, factual commit-activity heatmap."""
    weeks = len(grid[0]) if grid else 0
    cell = 11
    gap = 3
    step = cell + gap
    left = 42
    top = 26
    width = max(240, left + weeks * step - gap + 12)
    height = top + 7 * step - gap + 30
    maximum = max((value for row in grid for value in row), default=0)
    total = sum(sum(row) for row in grid)
    range_end = start + timedelta(weeks=weeks - 1, days=6) if weeks else start

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        'role="img" aria-labelledby="activity-title activity-desc">',
        '<title id="activity-title">Public commit activity</title>',
        (
            '<desc id="activity-desc">Public commits on default branches from '
            f"{start.isoformat()} to {range_end.isoformat()}: {total} total.</desc>"
        ),
        (
            f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" '
            'rx="6" fill="#ffffff" stroke="#d0d7de"/>'
        ),
    ]

    previous_month = None
    for column in range(weeks):
        week_date = start + timedelta(weeks=column)
        month = week_date.strftime("%b")
        if column == 0 or month != previous_month:
            parts.append(
                f'<text x="{left + column * step}" y="16" font-size="9" '
                f'fill="#57606a" font-family="Arial, sans-serif">{month}</text>'
            )
        previous_month = month

    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        parts.append(
            f'<text x="{left - 6}" y="{top + row * step + 9}" font-size="9" '
            f'fill="#57606a" text-anchor="end" '
            f'font-family="Arial, sans-serif">{label}</text>'
        )

    for row in range(7):
        for column in range(weeks):
            count = grid[row][column]
            day = start + timedelta(weeks=column, days=row)
            parts.append(
                f'<rect x="{left + column * step}" y="{top + row * step}" '
                f'width="{cell}" height="{cell}" rx="2" '
                f'fill="{_activity_color(count, maximum)}">'
                f"<title>{day.isoformat()}: {_format_count(count, 'commit')}</title>"
                "</rect>"
            )

    legend_y = height - 12
    parts.append(
        f'<text x="{left}" y="{legend_y}" font-size="9" fill="#57606a" '
        f'font-family="Arial, sans-serif">Less</text>'
    )
    legend_colors = ("#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39")
    for index, color in enumerate(legend_colors):
        x = left + 26 + index * (cell + 3)
        parts.append(
            f'<rect x="{x}" y="{legend_y - 9}" width="{cell}" height="{cell}" '
            f'rx="2" fill="{color}"/>'
        )
    parts.append(
        f'<text x="{left + 26 + len(legend_colors) * (cell + 3) + 2}" y="{legend_y}" '
        f'font-size="9" fill="#57606a" font-family="Arial, sans-serif">More</text>'
    )
    parts.append(
        f'<text x="{width - 12}" y="{legend_y}" font-size="9" fill="#57606a" '
        f'text-anchor="end" font-family="Arial, sans-serif">'
        f"{_format_count(total, 'commit')}</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def render_commit_activity(
    repos: Sequence[Repo],
    now: datetime,
    image_path: str,
    weeks: int = HEATMAP_WEEKS,
) -> tuple[str, str | None]:
    if not any(repo.activity_weeks or repo.commits for repo in repos):
        return "_Commit activity data is temporarily unavailable._", None

    stats_repos = sum(1 for repo in repos if repo.activity_weeks)
    fallback_repos = sum(
        1 for repo in repos if not repo.activity_weeks and repo.commits
    )
    if stats_repos and fallback_repos:
        source = "repository statistics + recent commit history"
    elif fallback_repos:
        source = "recent commit history"
    else:
        source = "repository statistics"

    start, week_end, grid = aggregate_commit_activity(repos, now, weeks)
    svg = render_activity_svg(start, grid)
    digest = hashlib.sha256(svg.encode("utf-8")).hexdigest()[:8]
    separator = "&" if "?" in image_path else "?"
    image_url = f"{image_path}{separator}v={digest}"
    total = sum(sum(row) for row in grid)
    range_end = week_end + timedelta(days=6)
    caption = (
        f"_Public commits on default branches, {start.isoformat()} through "
        f"{range_end.isoformat()} ({_format_count(total, 'commit')}; "
        f"source: {source})._"
    )
    markdown = (
        f"![Public commit activity over the last {weeks} weeks]({image_url})\n\n{caption}"
    )
    return markdown, svg


# ---------------------------------------------------------------------------
# README assembly
# ---------------------------------------------------------------------------


def replace_section(text: str, name: str, body: str) -> str:
    """Replace exactly one delimited section without touching anything else."""
    start = f"<!-- {name}:start -->"
    end = f"<!-- {name}:end -->"
    start_count = text.count(start)
    end_count = text.count(end)

    if start_count != 1 or end_count != 1:
        raise SectionError(
            f"README section '{name}' must contain exactly one start marker and "
            f"one end marker (found {start_count} start, {end_count} end)."
        )

    start_index = text.index(start)
    end_index = text.index(end)
    if end_index < start_index:
        raise SectionError(f"README section '{name}' has its end marker before its start marker.")

    replacement = body.strip() or "_No data available._"
    return (
        text[: start_index + len(start)]
        + "\n\n"
        + replacement
        + "\n\n"
        + text[end_index:]
    )


def build_sections(
    repos: Sequence[Repo],
    now: datetime,
    summaries: dict[str, str],
    image_path: str,
) -> tuple[dict[str, str], str | None, list[Repo]]:
    ranked = rank_repositories(repos, now)
    featured = ranked[:FEATURED_LIMIT]
    activity_markdown, svg = render_commit_activity(ranked, now, image_path)

    bodies = {
        "featured-projects": render_featured(featured, summaries),
        "project-index": render_project_index(ranked, featured, summaries),
        "recent-activity": render_recent_activity(ranked),
        "latest-releases": render_releases(ranked),
        "repository-signals": render_repository_signals(ranked),
        "commit-activity": activity_markdown,
    }
    return bodies, svg, ranked


def update_readme_content(
    readme: str,
    repos: Sequence[Repo],
    now: datetime,
    summaries: dict[str, str],
    image_path: str,
) -> tuple[str, str | None, list[Repo]]:
    bodies, svg, ranked = build_sections(repos, now, summaries, image_path)
    updated = readme
    for name in SECTION_NAMES:
        updated = replace_section(updated, name, bodies[name])
    return updated, svg, ranked


# ---------------------------------------------------------------------------
# File I/O and CLI
# ---------------------------------------------------------------------------


def load_readme(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"README not found at {path}. Restore it from git history; the updater "
            "will not create or overwrite manual content."
        ) from exc


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh the generated sections of the profile README."
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=None,
        help="Path to README.md (default: $GITHUB_WORKSPACE/README.md or ./README.md).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated README to stdout without writing files.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if generated content differs from the files on disk.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)
    if args.dry_run and args.check:
        parser.error("--dry-run and --check cannot be used together")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or ".")
    readme_path = args.readme or (workspace / "README.md")
    assets_path = readme_path.parent / "assets" / "commit-activity.svg"
    image_path = assets_path.relative_to(readme_path.parent).as_posix()

    try:
        readme = load_readme(readme_path)

        owner = os.environ.get("GITHUB_REPOSITORY_OWNER") or DEFAULT_OWNER
        repository = os.environ.get("GITHUB_REPOSITORY") or ""
        profile_repo = (
            repository.split("/", 1)[1]
            if "/" in repository
            else DEFAULT_PROFILE_REPO
        )
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

        client = GitHubClient(token=token)
        raw_repositories = discover_public_repositories(client, owner, profile_repo)
        log.info("Discovered %d public repositories.", len(raw_repositories))

        repositories: list[Repo] = []
        for item in raw_repositories:
            try:
                repositories.append(collect_repository(client, owner, item))
            except Exception as exc:  # one malformed repo must not abort the run
                name = item.get("name", "<unnamed>") if isinstance(item, dict) else "<invalid>"
                log.warning("Skipping repository %s: %s", name, exc)

        if raw_repositories and not repositories:
            raise GitHubError(
                "Every repository fetch failed; refusing to overwrite the README."
            )

        now = datetime.now(timezone.utc)
        summaries = load_project_summaries()
        updated, svg, ranked = update_readme_content(
            readme, repositories, now, summaries, image_path
        )

        if args.check:
            svg_changed = False
            if svg is not None:
                existing_svg = (
                    assets_path.read_text(encoding="utf-8")
                    if assets_path.exists()
                    else None
                )
                svg_changed = existing_svg != svg
            if updated == readme and not svg_changed:
                log.info("README is up to date.")
                return 0
            log.info("README is out of date.")
            return 1

        if args.dry_run:
            print(updated)
            log.info("Dry run complete; no files were written.")
            return 0

        write_atomic(readme_path, updated)
        if svg is not None:
            write_atomic(assets_path, svg)

        log.info("Profile README updated: %s", readme_path)
        log.info("Repositories checked: %d", len(repositories))
        log.info(
            "Published releases found: %d",
            sum(len(repo.releases) for repo in repositories),
        )
        for repo in ranked[:FEATURED_LIMIT]:
            log.info(
                "Featured: %s (score %.2f, %d file(s))",
                repo.name,
                repo.score,
                repo.file_count,
            )
        return 0

    except (GitHubError, SectionError, OSError, ValueError) as exc:
        log.error("Update failed: %s", exc)
        return 1
    except Exception as exc:  # pragma: no cover - final safety net for CI
        log.exception("Unexpected failure: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
