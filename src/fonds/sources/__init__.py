"""Sources: where a workspace's repos come from."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Protocol

import click

if TYPE_CHECKING:
    from ..repo import Repo
    from ..workspace import SourceConfig, Workspace


class Detail(enum.Enum):
    """How much per-repo metadata to ask for.

    `clone` needs a name and a commit id for a few hundred repos and should not
    pay for language breakdowns; `inventory` needs everything. Keeping these
    distinct is the difference between a fast clone and a slow one.
    """

    BASIC = "basic"
    FULL = "full"


class Source(Protocol):
    key: str
    owner: str

    def list_repos(self, detail: Detail = Detail.BASIC) -> list[Repo]: ...

    def get_repos(self, names: list[str], detail: Detail = Detail.BASIC) -> list[Repo]: ...


def build_source(config: SourceConfig, workspace: Workspace) -> Source:
    if config.type == "github":
        from .github import GitHubSource

        return GitHubSource(config)
    raise click.ClickException(f"Unknown source type: {config.type!r}")
