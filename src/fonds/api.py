"""The public surface a plugin builds against.

A plugin is a module that defines a module-level `PLUGIN = Plugin(...)`. It
contributes through four hooks, each a tuple of instances rather than a class to
subclass — features are composed, not inherited:

    commands      click commands added to the `fonds` CLI
    columns       columns added to the inventory table
    checks        assertions run by `fonds check`
    requires      external binaries reported by `fonds doctor`

Built-in features are ordinary plugins living in `fonds/plugins/`; there is no
privileged path. Third parties register via the `fonds.plugins` entry point
group, and a workspace can drop a `.py` file in its own `plugins/` directory.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from .repo import Repo
    from .tags import TagStore
    from .workspace import Workspace


@dataclass
class Context:
    """What a column or check gets to work with.

    `existing` holds the row this repo had in the previous inventory, if any.
    Slow columns use it to fall back to a cached value rather than record a
    transient network failure as fact.
    """

    workspace: Workspace
    repos: list[Repo] = field(default_factory=list)
    existing: dict = field(default_factory=dict)
    _tag_store: TagStore | None = None

    @property
    def tags(self) -> TagStore:
        if self._tag_store is None:
            from .tags import build_tag_store

            self._tag_store = build_tag_store(self.workspace)
        return self._tag_store

    def source_for(self, repo: Repo):
        """The source that produced *repo*."""
        for source in self.workspace.sources:
            if source.key == repo.source_key:
                return source
        return self.workspace.sources[0]

    def settings(self, plugin: str) -> dict:
        return self.workspace.plugin_settings(plugin)


@dataclass(frozen=True)
class Requirement:
    """An external binary a feature needs."""

    binary: str
    install: str = ""
    url: str = ""
    used_by: str = ""

    def find(self) -> str | None:
        return shutil.which(self.binary)

    def resolve(self) -> str:
        """Return the binary's path, or raise with install instructions."""
        path = self.find()
        if path:
            return path
        hint = f" Install it with: {self.install}" if self.install else ""
        link = f" ({self.url})" if self.url else ""
        raise click.ClickException(f"{self.binary} not found on PATH.{hint}{link}")


@dataclass(frozen=True)
class Column:
    """A column in the inventory table.

    `value` renders one cell. `slow` marks columns whose value costs a network
    round trip per repo: those are reused from the previous inventory whenever
    the repo has not changed since it was last written.
    """

    name: str
    value: Callable[[Repo, Context], Any]
    slow: bool = False
    width: int = 20
    center: bool = False


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str = ""
    details: Sequence[str] = ()


@dataclass(frozen=True)
class Check:
    """An assertion about the workspace, run by `fonds check`.

    Checks are what make a nightly job worth having: they turn the inventory
    from a report into something that can fail.
    """

    name: str
    run: Callable[[Context], CheckResult]
    help: str = ""


@dataclass(frozen=True)
class Plugin:
    name: str
    help: str = ""
    commands: tuple[click.Command, ...] = ()
    columns: tuple[Column, ...] = ()
    checks: tuple[Check, ...] = ()
    requires: tuple[Requirement, ...] = ()


def ok(message: str = "") -> CheckResult:
    return CheckResult(ok=True, message=message)


def failed(message: str, details: Sequence[str] = ()) -> CheckResult:
    return CheckResult(ok=False, message=message, details=details)


__all__ = [
    "Check",
    "CheckResult",
    "Column",
    "Context",
    "Plugin",
    "Requirement",
    "failed",
    "ok",
]
