"""Workspace discovery, configuration, and conventional paths.

A workspace is any directory containing a `fonds.toml`. Commands find it by
walking up from the current directory, the way git finds `.git` — so the tool
is installed once and operates on whichever workspace you are standing in,
rather than being bound to the checkout it happens to live in.

Layout is convention, not configuration:

    fonds.toml          the marker file, plus whatever little config is needed
    repos/              checkouts of every repo in the workspace
    wikis/              checkouts of GitHub wikis
    inventory/          all.md, plus one file per tag
    tags/<tag>/         optional focused checkouts of a tagged subset
    data/<plugin>/      artifacts a plugin caches between runs
    local/              git-ignored scratch space
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import click

CONFIG_NAME = "fonds.toml"
ENV_WORKSPACE = "FONDS_WORKSPACE"


@dataclass(frozen=True)
class SourceConfig:
    """One place repos come from."""

    owner: str
    type: str = "github"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.owner


@dataclass
class Config:
    sources: tuple[SourceConfig, ...] = ()
    settings: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, data: dict, path: Path) -> Config:
        """Build a Config from parsed `fonds.toml` data.

        `owner = "x"` is sugar for a single source; `[[sources]]` is the long
        form. They are mutually exclusive so there is only one place to look.
        """
        owner = data.get("owner")
        raw_sources = data.get("sources", [])

        if owner and raw_sources:
            raise click.ClickException(
                f"{path}: set either `owner` or `[[sources]]`, not both."
            )
        if owner:
            raw_sources = [{"owner": owner}]
        if not raw_sources:
            raise click.ClickException(
                f"{path}: no repo sources configured. Add `owner = \"<github-user-or-org>\"`."
            )

        sources = []
        for entry in raw_sources:
            if "owner" not in entry:
                raise click.ClickException(f"{path}: every source needs an `owner`.")
            sources.append(
                SourceConfig(
                    owner=entry["owner"],
                    type=entry.get("type", "github"),
                    include=tuple(entry.get("include", ())),
                    exclude=tuple(entry.get("exclude", ())),
                )
            )

        settings = {
            key: value
            for key, value in data.items()
            if key not in {"owner", "sources"}
        }
        return cls(sources=tuple(sources), settings=settings)


class Workspace:
    """A metarepo workspace rooted at a directory containing `fonds.toml`."""

    def __init__(self, root: Path, config: Config):
        self.root = root
        self.config = config

    # -- discovery ---------------------------------------------------------

    @classmethod
    def find(cls, start: Path | None = None) -> Workspace:
        """Locate the enclosing workspace, or raise a helpful error."""
        root = cls.find_root(start)
        if root is None:
            searched = Path(start or os.getcwd()).resolve()
            raise click.ClickException(
                f"No {CONFIG_NAME} found in {searched} or any parent directory.\n"
                f"Run `fonds init <owner>` to create a workspace here."
            )
        return cls.load(root)

    @classmethod
    def find_root(cls, start: Path | None = None) -> Path | None:
        if env_root := os.environ.get(ENV_WORKSPACE):
            candidate = Path(env_root).resolve()
            if (candidate / CONFIG_NAME).is_file():
                return candidate
            raise click.ClickException(
                f"{ENV_WORKSPACE} is set to {candidate}, which has no {CONFIG_NAME}."
            )
        current = Path(start or os.getcwd()).resolve()
        for directory in [current, *current.parents]:
            if (directory / CONFIG_NAME).is_file():
                return directory
        return None

    @classmethod
    def load(cls, root: Path) -> Workspace:
        path = root / CONFIG_NAME
        data = tomllib.loads(path.read_text())
        return cls(root, Config.parse(data, path))

    # -- conventional paths ------------------------------------------------

    @property
    def repos_dir(self) -> Path:
        return self.root / "repos"

    @property
    def wikis_dir(self) -> Path:
        return self.root / "wikis"

    @property
    def inventory_dir(self) -> Path:
        return self.root / "inventory"

    @property
    def inventory_path(self) -> Path:
        return self.inventory_dir / "all.md"

    @property
    def local_dir(self) -> Path:
        return self.root / "local"

    def tag_dir(self, tag: str) -> Path:
        return self.root / "tags" / tag

    def wiki_tag_dir(self, tag: str) -> Path:
        return self.wikis_dir / tag

    def data_dir(self, plugin: str, create: bool = False) -> Path:
        """Where *plugin* caches artifacts between runs."""
        path = self.root / "data" / plugin
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    # -- config accessors --------------------------------------------------

    def setting(self, key: str, default=None):
        """Read a top-level setting, e.g. `workspace.setting("guards")`."""
        return self.config.settings.get(key, default)

    def plugin_settings(self, plugin: str) -> dict:
        """Read the `[<plugin>]` table from fonds.toml, or an empty dict."""
        value = self.config.settings.get(plugin, {})
        return value if isinstance(value, dict) else {}

    @cached_property
    def sources(self) -> list:
        from .sources import build_source

        return [build_source(config, self) for config in self.config.sources]

    def __repr__(self) -> str:
        return f"<Workspace {self.root}>"
