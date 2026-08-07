"""Tagging repos, over whatever GitHub gives us to tag with.

Organizations have a `tags` custom property with a schema of allowed values.
User accounts have no custom properties at all, so there we use repo topics,
which are already returned by the bulk listing and so cost nothing to read.

Which backend applies is decided by asking GitHub what kind of owner this is —
there is nothing to configure.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

import click

from .sources.github import GitHubSource, rest_request

if TYPE_CHECKING:
    from .workspace import Workspace

TAGS_PROPERTY = "tags"

# GitHub's rules for topics: lowercase alphanumerics and hyphens, starting with
# an alphanumeric, up to 50 characters.
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")


class TagStore(Protocol):
    backend: str

    def get_all(self) -> dict[str, list[str]]: ...

    def allowed(self) -> list[str]: ...

    def set(self, repo_name: str, tags: list[str]) -> None: ...


class CustomPropertyTags:
    """Org tagging via the `tags` custom property."""

    backend = "custom property"

    def __init__(self, source: GitHubSource):
        self.source = source
        self.owner = source.owner

    def get_all(self) -> dict[str, list[str]]:
        from .sources.github import rest_pages

        result: dict[str, list[str]] = {}
        path = f"/orgs/{self.owner}/properties/values?per_page=100"
        for entry in rest_pages(path):
            values = {
                prop["property_name"]: prop.get("value")
                for prop in entry.get("properties", [])
            }
            result[entry["repository_name"]] = _coerce(values.get(TAGS_PROPERTY))
        return result

    def allowed(self) -> list[str]:
        schema = rest_request(
            "GET", f"/orgs/{self.owner}/properties/schema/{TAGS_PROPERTY}"
        )
        return sorted(schema.get("allowed_values") or [])

    def set(self, repo_name: str, tags: list[str]) -> None:
        rest_request(
            "PATCH",
            f"/orgs/{self.owner}/properties/values",
            {
                "repository_names": [repo_name],
                "properties": [
                    {"property_name": TAGS_PROPERTY, "value": sorted(tags) or None}
                ],
            },
        )


class TopicTags:
    """User-account tagging via repo topics.

    Topics are a flat, open vocabulary: `allowed()` is simply what is in use.
    """

    backend = "topics"

    def __init__(self, source: GitHubSource):
        self.source = source
        self.owner = source.owner
        self._cache: dict[str, list[str]] | None = None

    def get_all(self) -> dict[str, list[str]]:
        # Topics arrive with the bulk listing, so this is one request either
        # way — but `allowed()` derives from it, so don't fetch twice.
        if self._cache is None:
            self._cache = {
                repo.name: list(repo.tags) for repo in self.source.list_repos()
            }
        return self._cache

    def allowed(self) -> list[str]:
        return sorted({tag for tags in self.get_all().values() for tag in tags})

    def set(self, repo_name: str, tags: list[str]) -> None:
        invalid = [tag for tag in tags if not TOPIC_RE.match(tag)]
        if invalid:
            raise click.ClickException(
                f"Not valid GitHub topics: {', '.join(invalid)}. Topics must be "
                "lowercase alphanumerics and hyphens, starting with a letter or digit."
            )
        if len(tags) > 20:
            raise click.ClickException(
                f"{repo_name}: GitHub allows at most 20 topics per repo (got {len(tags)})."
            )
        rest_request(
            "PUT",
            f"/repos/{self.owner}/{repo_name}/topics",
            {"names": sorted(tags)},
        )
        if self._cache is not None:
            self._cache[repo_name] = sorted(tags)


def _coerce(value) -> list[str]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str) and value:
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


class MultiSourceTags:
    """Fans reads and writes out to the right store for each source."""

    def __init__(self, stores: dict[str, TagStore], repo_owners: dict[str, str]):
        self.stores = stores
        self.repo_owners = repo_owners

    @property
    def backend(self) -> str:
        return ", ".join(sorted({store.backend for store in self.stores.values()}))

    def get_all(self) -> dict[str, list[str]]:
        merged: dict[str, list[str]] = {}
        for store in self.stores.values():
            merged.update(store.get_all())
        return merged

    def allowed(self) -> list[str]:
        return sorted({tag for store in self.stores.values() for tag in store.allowed()})

    def set(self, repo_name: str, tags: list[str]) -> None:
        owner = self.repo_owners.get(repo_name)
        if owner is None:
            if len(self.stores) == 1:
                next(iter(self.stores.values())).set(repo_name, tags)
                return
            raise click.ClickException(
                f"Don't know which source owns {repo_name!r}; run `fonds inventory` first."
            )
        self.stores[owner].set(repo_name, tags)


def store_for(source: GitHubSource) -> TagStore:
    return CustomPropertyTags(source) if source.is_org else TopicTags(source)


def build_tag_store(workspace: Workspace) -> TagStore:
    sources = workspace.sources
    if len(sources) == 1:
        return store_for(sources[0])
    stores = {source.owner: store_for(source) for source in sources}
    repo_owners = {
        repo.name: repo.owner
        for source in sources
        for repo in source.list_repos()
    }
    return MultiSourceTags(stores, repo_owners)


def parse_tags(raw: str) -> list[str]:
    """Parse a comma-separated tag string into a sorted, deduplicated list."""
    return sorted({tag.strip() for tag in raw.split(",") if tag.strip()})
