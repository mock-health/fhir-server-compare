"""Print the latest GitHub release tag for each roster server.

Hand-refresh flow: run this, eyeball the tags, then update `version:` in
servers.yaml. We don't write YAML back automatically so you can sanity-check
release notes (breaking changes, deprecated configs) before bumping the matrix.

For servers without a GitHub release feed:
  - aidbox: commercial, no upstream repo. Version tracked as "latest" in yaml;
    see https://docs.aidbox.app for the changelog.

Usage:
  python scripts/fetch_server_versions.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

# owner/repo for each roster server with a public GitHub release feed.
# Keep the roster-vs-source-repo mapping here (not servers.yaml) to avoid
# cluttering the runtime config with publisher-side metadata.
REPOS: dict[str, tuple[str, str]] = {
    "hapi":    ("hapifhir", "hapi-fhir-jpaserver-starter"),
    "msfhir":  ("microsoft", "fhir-server"),
    "medplum": ("medplum", "medplum"),
    "blaze":   ("samply", "blaze"),
    "spark":   ("FirelyTeam", "spark"),
}


def latest_release(owner: str, repo: str) -> tuple[str | None, str | None]:
    """Return (tag_name, published_at_iso) for the latest release, or (None, None)."""
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "mock.health", "Accept": "application/vnd.github+json"},
    )
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    except urllib.error.HTTPError as e:
        print(f"[warn] {owner}/{repo}: HTTP {e.code} {e.reason}", file=sys.stderr)
        return None, None
    except Exception as e:
        print(f"[warn] {owner}/{repo}: {e}", file=sys.stderr)
        return None, None
    return data.get("tag_name"), data.get("published_at")


def normalize(sid: str, tag: str) -> str:
    """Strip common release-tag prefixes so the version reads cleanly in the UI.

    Keeps suffixes like -r4 or -1 because they're meaningful (R4 profile,
    image-rebuild revision), unlike a bare 'v' which just clutters the display.
    """
    for prefix in ("image/v", "release/", "v"):
        if tag.startswith(prefix):
            return tag[len(prefix):]
    return tag


def main() -> None:
    print(f"{'server':8s}  {'tag':22s}  normalized  published")
    print("-" * 70)
    for sid, (owner, repo) in REPOS.items():
        tag, published = latest_release(owner, repo)
        if tag is None:
            print(f"{sid:8s}  {'(fetch failed)':22s}")
            continue
        print(f"{sid:8s}  {tag:22s}  {normalize(sid, tag):10s}  {published}")
    print()
    print("aidbox  — commercial, no release feed; keep version: 'latest' "
          "and rely on source_url for changelog")


if __name__ == "__main__":
    main()
