"""Choosing which repos a command acts on.

Nearly every command wants the same three filters — some repos by name, or a
tag, or a whole source — resolved against either the remote listing or the
local checkouts. Putting that in one place is what keeps `--tag ops` meaning
exactly the same thing to `clone`, `secrets`, `deps`, and `hydrate`.

Local selection prefers the inventory file over the network, so filtering
checkouts by tag works offline.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

import click

from .repo import Checkout, Repo, assign_dir_names
from .sources import Detail
from .table import read_table, row_tags
from .workspace import Workspace


@dataclass
class Selection:
    workspace: Workspace
    repo_names: tuple[str, ...] = ()
    tag: str | None = None
    source_keys: tuple[str, ...] = ()

    # -- sources -----------------------------------------------------------

    @property
    def sources(self) -> list:
        sources = self.workspace.sources
        if not self.source_keys:
            return sources
        chosen = [s for s in sources if s.key in self.source_keys]
        unknown = set(self.source_keys) - {s.key for s in sources}
        if unknown:
            raise click.ClickException(
                f"Unknown source(s): {', '.join(sorted(unknown))}. "
                f"Configured: {', '.join(s.key for s in sources)}"
            )
        return chosen

    # -- remote ------------------------------------------------------------

    def remote(self, detail: Detail = Detail.BASIC) -> list[Repo]:
        """Repos as the sources describe them, filtered by this selection."""
        repos: list[Repo] = []
        for source in self.sources:
            if self.repo_names:
                # Ask only the sources that actually have these names, so a
                # multi-source workspace doesn't error on the other one.
                available = {r.name for r in source.list_repos()}
                wanted = [n for n in self.repo_names if n in available]
                if wanted:
                    repos.extend(source.get_repos(wanted, detail))
            else:
                repos.extend(source.list_repos(detail))

        if self.repo_names:
            missing = set(self.repo_names) - {r.name for r in repos}
            if missing:
                raise click.ClickException(
                    f"Repo(s) not found: {', '.join(sorted(missing))}"
                )
        if self.tag:
            repos = [r for r in repos if self.tag in r.tags]

        assign_dir_names(repos)
        return sorted(repos, key=lambda r: r.dir_name.lower())

    # -- local -------------------------------------------------------------

    def checkout_root(self, output_dir: Path | None = None) -> Path:
        """Where checkouts for this selection live.

        `--tag ops` gets its own `tags/ops/` tree, which is what makes a
        focused subset of a large workspace cheap to keep around.
        """
        if output_dir:
            return output_dir.resolve()
        if self.tag:
            return self.workspace.tag_dir(self.tag)
        return self.workspace.repos_dir

    def local(self, base: Path | None = None) -> list[Checkout]:
        """Existing checkouts matching this selection."""
        root = self.checkout_root(base)
        if not root.is_dir():
            raise click.ClickException(f"No checkouts directory at {root}")

        present = sorted(
            (p for p in root.iterdir() if p.is_dir() and p.name != ".gitkeep"),
            key=lambda p: p.name.lower(),
        )

        if self.repo_names:
            wanted = set(self.repo_names)
            missing = wanted - {p.name for p in present}
            if missing:
                raise click.ClickException(
                    f"No checkout for: {', '.join(sorted(missing))} (in {root})"
                )
            present = [p for p in present if p.name in wanted]
        elif self.tag and base is None and root == self.workspace.repos_dir:
            # A tag applied to repos/ filters by tag; a tags/<tag>/ tree is
            # already filtered by construction.
            tagged = self.tagged_names()
            present = [p for p in present if p.name in tagged]
            if not present:
                raise click.ClickException(f"No local checkouts matching tag {self.tag!r}")

        return [Checkout(name=p.name, path=p) for p in present]

    def tagged_names(self) -> set[str]:
        """Names carrying `self.tag`, from the inventory if we have one."""
        if not self.tag:
            return set()
        path = self.workspace.inventory_path
        if path.exists():
            rows = read_table(path)
            if rows and "Tags" in rows[0]:
                return {
                    row["Name"]
                    for row in rows
                    if row.get("Name") and self.tag in row_tags(row)
                }
        return {repo.name for repo in self.remote() if self.tag in repo.tags}

    def default_branches(self) -> dict[str, str]:
        """Repo name -> default branch, read from the inventory (no network)."""
        path = self.workspace.inventory_path
        if not path.exists():
            return {}
        rows = read_table(path)
        if rows and "Default Branch" not in rows[0]:
            return {}
        return {
            row["Name"]: row["Default Branch"]
            for row in rows
            if row.get("Name") and row.get("Default Branch")
        }


def repo_options(func):
    """Add `--repo`, `--tag`, and `--source`, and pass a `Selection`.

    The decorated command receives `selection` in place of the raw options.
    """

    @click.option(
        "--repo",
        "repo_names",
        multiple=True,
        metavar="NAME",
        help="Limit to specific repo(s). Repeatable.",
    )
    @click.option("--tag", default=None, help="Limit to repos carrying this tag.")
    @click.option(
        "--source",
        "source_keys",
        multiple=True,
        metavar="OWNER",
        help="Limit to specific configured source(s). Repeatable.",
    )
    @functools.wraps(func)
    def wrapper(*args, repo_names, tag, source_keys, **kwargs):
        selection = Selection(
            workspace=Workspace.find(),
            repo_names=repo_names,
            tag=tag,
            source_keys=source_keys,
        )
        return func(*args, selection=selection, **kwargs)

    return wrapper
