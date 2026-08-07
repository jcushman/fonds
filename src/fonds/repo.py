"""The core domain object: a repo, as described by some source."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path


@dataclass
class Repo:
    """A repo as described by a source.

    Sources fill in what they can cheaply; `Detail.BASIC` leaves the
    inventory-only fields at their defaults. `name` is unique within a source,
    and `dir_name` is unique within a workspace.
    """

    name: str
    owner: str
    source_key: str = ""

    # Cheap fields, always populated.
    default_branch: str = ""
    default_branch_oid: str | None = None
    archived: bool = False
    tags: list[str] = field(default_factory=list)

    # Fuller detail, populated for Detail.FULL.
    description: str = ""
    url: str = ""
    private: bool = False
    fork: bool = False
    stars: int = 0
    forks: int = 0
    updated_at: datetime | None = None
    has_wiki: bool = False
    has_pages: bool = False
    languages: dict[str, int] = field(default_factory=dict)
    other_branches: str = ""

    # Set by `assign_dir_names`; see `dir_name`.
    _dir_name: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def dir_name(self) -> str:
        """Directory name for this repo's checkout, relative to repos/.

        Flat by default: a repo is `repos/<name>`, not `repos/<owner>/<name>`.
        Workspaces with more than one source get an owner prefix only for the
        names that actually collide (see `assign_dir_names`).
        """
        return self._dir_name or self.name

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.full_name}.git"

    @property
    def wiki_clone_url(self) -> str:
        return f"https://github.com/{self.full_name}.wiki.git"

    @property
    def html_url(self) -> str:
        return self.url or f"https://github.com/{self.full_name}"


@dataclass(frozen=True)
class Checkout:
    """A local git checkout of a repo."""

    name: str
    path: Path

    @property
    def exists(self) -> bool:
        return (self.path / ".git").exists()


def matches_patterns(
    name: str,
    include: list[str] | tuple[str, ...] = (),
    exclude: list[str] | tuple[str, ...] = (),
) -> bool:
    """True if *name* passes an include/exclude glob filter.

    An empty *include* means "everything"; *exclude* always wins.
    """
    if include and not any(fnmatch(name, pattern) for pattern in include):
        return False
    if any(fnmatch(name, pattern) for pattern in exclude):
        return False
    return True


def assign_dir_names(repos: list[Repo]) -> list[Repo]:
    """Give every repo a unique `dir_name`, prefixing only on collision.

    Single-source workspaces — the overwhelmingly common case — keep the flat
    `repos/<name>` layout. Only names claimed by two different owners get
    disambiguated to `<owner>__<name>`, so a second source never imposes an
    extra directory level on repos that don't need one.
    """
    owners_by_name: dict[str, set[str]] = {}
    for repo in repos:
        owners_by_name.setdefault(repo.name, set()).add(repo.owner)

    for repo in repos:
        if len(owners_by_name[repo.name]) > 1:
            repo._dir_name = f"{repo.owner}__{repo.name}"
        else:
            repo._dir_name = repo.name
    return repos
