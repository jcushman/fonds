"""GitHub as a source of repos.

Everything goes through `repositoryOwner`, which resolves both users and
organizations, so a personal account and an org take exactly the same code
path. The one place they genuinely differ is tagging — orgs have custom
properties, users do not — and that lives in `fonds/tags.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from functools import cache, cached_property

import click

from ..repo import Repo, matches_patterns
from ..workspace import SourceConfig
from . import Detail

API_ROOT = "https://api.github.com"
TOKEN_ENV = "GITHUB_TOKEN"

# Branches worth naming in the inventory; anything else is just a count.
BRANCHES_TO_SURFACE = ("prod", "staging", "develop", "main", "master")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _gh_cli_token() -> str | None:
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True
        )
    except FileNotFoundError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


@cache
def get_token() -> str | None:
    """Token from the environment, then a token file, then the GitHub CLI."""
    if token := os.environ.get(TOKEN_ENV):
        return token
    from ..workspace import Workspace

    root = Workspace.find_root()
    if root and (token_file := root / ".github-token").is_file():
        return token_file.read_text().strip()
    return _gh_cli_token()


def require_token() -> str:
    token = get_token()
    if not token:
        raise click.ClickException(
            "No GitHub token found. Set GITHUB_TOKEN, run `gh auth login`, "
            "or write one to .github-token in the workspace."
        )
    return token


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {require_token()}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def graphql(query: str, variables: dict) -> dict:
    """Run a GraphQL query, retrying server errors with backoff."""
    last_error: Exception | None = None
    for attempt in range(3):
        request = urllib.request.Request(
            f"{API_ROOT}/graphql",
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers=headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
                break
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            last_error = click.ClickException(f"GitHub GraphQL request failed: {detail}")
            if e.code < 500 or attempt == 2:
                raise last_error from e
        except urllib.error.URLError as e:
            last_error = click.ClickException(f"GitHub GraphQL request failed: {e}")
            if attempt == 2:
                raise last_error from e
        time.sleep(2**attempt)
    else:  # pragma: no cover - loop always breaks or raises
        raise last_error or click.ClickException("GitHub GraphQL request failed.")

    if payload.get("errors"):
        messages = "; ".join(e.get("message", str(e)) for e in payload["errors"])
        raise click.ClickException(f"GitHub GraphQL request failed: {messages}")
    return payload["data"]


def _next_link(response_headers) -> str | None:
    link_header = response_headers.get("Link")
    if not link_header:
        return None
    for part in link_header.split(","):
        url_part, *params = part.split(";")
        if any(param.strip() == 'rel="next"' for param in params):
            return url_part.strip()[1:-1]
    return None


def rest_pages(path: str) -> list:
    """Follow REST pagination, returning every item."""
    url = path if path.startswith("https://") else f"{API_ROOT}{path}"
    items: list = []
    while url:
        request = urllib.request.Request(url, headers=headers())
        with urllib.request.urlopen(request, timeout=30) as response:
            items.extend(json.load(response))
            url = _next_link(response.headers)
    return items


def rest_request(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{API_ROOT}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers(),
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# GraphQL field sets
# ---------------------------------------------------------------------------

BASIC_FIELDS = """
    name
    isArchived
    defaultBranchRef { name target { oid } }
    repositoryTopics(first: 25) { nodes { topic { name } } }
"""


def _branch_alias_fields() -> tuple[str, dict[str, str]]:
    alias_to_branch = {f"branch{i}": name for i, name in enumerate(BRANCHES_TO_SURFACE)}
    fields = "\n".join(
        f'{alias}: refs(refPrefix: "refs/heads/", query: "{branch}", first: 10) '
        "{ nodes { name } }"
        for alias, branch in alias_to_branch.items()
    )
    return fields, alias_to_branch


def _full_fields() -> tuple[str, dict[str, str]]:
    branch_fields, alias_to_branch = _branch_alias_fields()
    fields = f"""
    {BASIC_FIELDS}
    description
    url
    isPrivate
    isFork
    stargazerCount
    forkCount
    updatedAt
    hasWikiEnabled
    languages(first: 20, orderBy: {{field: SIZE, direction: DESC}}) {{
      totalSize
      edges {{ size node {{ name }} }}
    }}
    refs(refPrefix: "refs/heads/", first: 1) {{ totalCount }}
    {branch_fields}
    """
    return fields, alias_to_branch


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _format_other_branches(
    total_count: int, default_branch: str, surfaced: set[str]
) -> str:
    named = [b for b in BRANCHES_TO_SURFACE if b != default_branch and b in surfaced]
    other = max(total_count - (1 if default_branch else 0) - len(named), 0)
    parts = list(named)
    if other:
        parts.append(f"{other} {'other' if other == 1 else 'others'}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

class GitHubSource:
    def __init__(self, config: SourceConfig):
        self.config = config
        self.owner = config.owner
        self.key = config.owner

    @cached_property
    def owner_type(self) -> str:
        """"User" or "Organization" — decided once, by asking GitHub."""
        data = graphql(
            "query($owner: String!) { repositoryOwner(login: $owner) { __typename } }",
            {"owner": self.owner},
        )
        owner = data.get("repositoryOwner")
        if not owner:
            raise click.ClickException(f"GitHub owner not found: {self.owner}")
        return owner["__typename"]

    @property
    def is_org(self) -> bool:
        return self.owner_type == "Organization"

    # -- listing -----------------------------------------------------------

    def list_repos(self, detail: Detail = Detail.BASIC) -> list[Repo]:
        fields, alias_to_branch = self._fields(detail)
        query = f"""
        query($owner: String!, $cursor: String) {{
          repositoryOwner(login: $owner) {{
            repositories(
              first: {50 if detail is Detail.FULL else 100}
              after: $cursor
              ownerAffiliations: OWNER
              orderBy: {{field: NAME, direction: ASC}}
            ) {{
              pageInfo {{ hasNextPage endCursor }}
              nodes {{ {fields} }}
            }}
          }}
        }}
        """
        repos: list[Repo] = []
        cursor = None
        while True:
            data = graphql(query, {"owner": self.owner, "cursor": cursor})
            page = data["repositoryOwner"]["repositories"]
            repos.extend(
                self._repo_from_node(node, alias_to_branch) for node in page["nodes"]
            )
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]

        repos = [
            repo
            for repo in repos
            if matches_patterns(repo.name, self.config.include, self.config.exclude)
        ]
        if detail is Detail.FULL:
            self._attach_pages_status(repos)
        return repos

    def get_repos(self, names: list[str], detail: Detail = Detail.BASIC) -> list[Repo]:
        """Fetch specific repos by name, in one aliased query."""
        if not names:
            return []
        fields, alias_to_branch = self._fields(detail)
        var_defs = ", ".join(
            ["$owner: String!"] + [f"$repo{i}: String!" for i in range(len(names))]
        )
        aliases = "\n".join(
            f"repo{i}: repository(name: $repo{i}) {{ {fields} }}"
            for i in range(len(names))
        )
        query = f"""
        query({var_defs}) {{
          repositoryOwner(login: $owner) {{
            {aliases}
          }}
        }}
        """
        variables = {"owner": self.owner} | {
            f"repo{i}": name for i, name in enumerate(names)
        }
        data = graphql(query, variables)["repositoryOwner"]

        repos, missing = [], []
        for i, name in enumerate(names):
            node = data.get(f"repo{i}")
            if node is None:
                missing.append(name)
            else:
                repos.append(self._repo_from_node(node, alias_to_branch))
        if missing:
            raise click.ClickException(
                f"Repo(s) not found in {self.owner}: {', '.join(sorted(missing))}"
            )
        if detail is Detail.FULL:
            self._attach_pages_status(repos)
        return sorted(repos, key=lambda r: r.name.lower())

    # -- internals ---------------------------------------------------------

    def _fields(self, detail: Detail) -> tuple[str, dict[str, str]]:
        return _full_fields() if detail is Detail.FULL else (BASIC_FIELDS, {})

    def _repo_from_node(self, node: dict, alias_to_branch: dict[str, str]) -> Repo:
        default_branch_ref = node.get("defaultBranchRef") or {}
        default_branch = default_branch_ref.get("name") or ""
        topics = [
            entry["topic"]["name"]
            for entry in (node.get("repositoryTopics") or {}).get("nodes", [])
        ]

        repo = Repo(
            name=node["name"],
            owner=self.owner,
            source_key=self.key,
            default_branch=default_branch,
            default_branch_oid=(default_branch_ref.get("target") or {}).get("oid"),
            archived=bool(node.get("isArchived")),
            tags=topics,
        )

        if "url" not in node:
            return repo

        surfaced = {
            branch
            for alias, branch in alias_to_branch.items()
            if any(
                ref.get("name") == branch
                for ref in (node.get(alias) or {}).get("nodes", [])
            )
        }
        languages = node.get("languages") or {}

        repo.description = node.get("description") or ""
        repo.url = node["url"]
        repo.private = bool(node.get("isPrivate"))
        repo.fork = bool(node.get("isFork"))
        repo.stars = node.get("stargazerCount") or 0
        repo.forks = node.get("forkCount") or 0
        repo.updated_at = _parse_datetime(node.get("updatedAt"))
        repo.has_wiki = bool(node.get("hasWikiEnabled"))
        repo.languages = {
            edge["node"]["name"]: edge["size"]
            for edge in languages.get("edges", [])
            if edge.get("node") and isinstance(edge.get("size"), int)
        }
        repo.other_branches = _format_other_branches(
            (node.get("refs") or {}).get("totalCount") or 0, default_branch, surfaced
        )
        return repo

    def _attach_pages_status(self, repos: list[Repo]) -> None:
        """Fill in `has_pages`, which GraphQL does not expose.

        One bulk REST listing rather than a request per repo.
        """
        if not repos:
            return
        if self.is_org:
            path = f"/orgs/{self.owner}/repos?per_page=100&type=all"
        else:
            # `/users/{login}/repos` omits private repos even for yourself.
            path = "/user/repos?per_page=100&affiliation=owner"

        by_name = {
            item["name"]: bool(item.get("has_pages"))
            for item in rest_pages(path)
            if item.get("owner", {}).get("login", "").lower() == self.owner.lower()
        }
        missing = sorted(repo.name for repo in repos if repo.name not in by_name)
        if missing:
            raise click.ClickException(
                "GitHub's repository listing omitted Pages status for: "
                + ", ".join(missing)
            )
        for repo in repos:
            repo.has_pages = by_name[repo.name]

    def contributors(self, repo: Repo) -> list[str]:
        path = f"/repos/{self.owner}/{urllib.parse.quote(repo.name)}/contributors?per_page=100"
        try:
            items = rest_pages(path)
        except (urllib.error.URLError, OSError, ValueError):
            return []
        return [
            item.get("login", "")
            for item in items
            if item.get("type") == "User" and item.get("login")
        ]
