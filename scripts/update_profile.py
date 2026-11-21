
#!/usr/bin/env python3
"""Update generated release and latest-commit sections in the profile README."""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

OWNER = "faseehabdullah57-blip"

REPOSITORIES = [
    "ghl-data-migration-tool",
    "ghl-lead-scoring-service",
    "ghl-webhook-processor.",
    "ghl-snapshot-validator",
    "leadflow-crm",
]

API = "https://api.github.com"


def api_get(path):
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "profile-readme-updater",
    }

    if token:
        headers["Authorization"] = "Bearer " + token

    request = urllib.request.Request(API + path, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"GitHub API HTTP {exc.code}: {detail}"
        ) from exc


def encoded_repo(repo):
    return urllib.parse.quote(repo, safe="")


def latest_release(repo):
    path = (
        f"/repos/{OWNER}/{encoded_repo(repo)}"
        "/releases?per_page=5"
    )

    releases = api_get(path)

    if not isinstance(releases, list):
        raise RuntimeError(f"Unexpected release response: {repo}")

    published = [
        item for item in releases
        if item.get("published_at") and not item.get("draft")
    ]

    return published[0] if published else None


def latest_commit(repo):
    path = (
        f"/repos/{OWNER}/{encoded_repo(repo)}"
        "/commits?per_page=1"
    )

    commits = api_get(path)

    if not isinstance(commits, list) or not commits:
        raise RuntimeError(f"No commit data: {repo}")

    return commits[0]


def replace_section(text, name, body):
    start = f"<!-- {name}:start -->"
    end = f"<!-- {name}:end -->"

    pattern = re.compile(
        re.escape(start) + r".*?" + re.escape(end),
        re.DOTALL,
    )

    replacement = f"{start}\n{body.strip()}\n{end}"

    updated, count = pattern.subn(
        lambda _: replacement,
        text,
        count=1,
    )

    if count != 1:
        raise RuntimeError(f"Missing README section: {name}")

    return updated


def main():
    workspace = os.environ.get("GITHUB_WORKSPACE", ".")
    readme_path = os.path.join(workspace, "README.md")

    with open(readme_path, encoding="utf-8") as file:
        readme = file.read()

    releases = []
    activity = []

    for repo in REPOSITORIES:
        release = latest_release(repo)

        if release:
            releases.append(
                (release.get("published_at", ""), repo, release)
            )

        commit = latest_commit(repo)
        commit_data = commit.get("commit", {})
        author = commit_data.get("author") or {}

        activity.append({
            "date": author.get("date", ""),
            "repo": repo,
            "sha": commit.get("sha", "")[:7],
            "message": (
                commit_data.get("message") or "Commit"
            ).splitlines()[0],
            "url": commit.get("html_url", ""),
        })

    releases.sort(key=lambda item: item[0], reverse=True)
    activity.sort(key=lambda item: item["date"], reverse=True)

    release_lines = []

    for published, repo, release in releases[:5]:
        date = datetime.fromisoformat(
            published.replace("Z", "+00:00")
        ).date().isoformat()

        title = (
            release.get("name")
            or release.get("tag_name")
            or "Release"
        )

        url = release.get("html_url", "")

        release_lines.append(
            f"- [{repo} — {title}]({url}) · {date}"
        )

    release_body = (
        "\n".join(release_lines)
        if release_lines
        else "_No published releases found yet._"
    )

    activity_lines = []

    for item in activity[:5]:
        date = item["date"][:10] or "Date unavailable"

        message = (
            item["message"]
            .replace("[", r"\[")
            .replace("]", r"\]")
        )

        activity_lines.append(
            f"- [{item['repo']}]({item['url']}) · "
            f"{date} · {message} (`{item['sha']}`)"
        )

    activity_body = (
        "\n".join(activity_lines)
        if activity_lines
        else "_No recent activity available._"
    )

    readme = replace_section(
        readme, "recent-releases", release_body
    )

    readme = replace_section(
        readme, "recent-activity", activity_body
    )

    with open(readme_path, "w", encoding="utf-8") as file:
        file.write(readme)

    print("Profile README successfully updated.")
    print(f"Repositories checked: {len(REPOSITORIES)}")
    print(f"Published releases found: {len(releases)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Update failed: {error}", file=sys.stderr)
        sys.exit(1)
