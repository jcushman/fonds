"""The `fonds` command.

Almost nothing is defined here: the CLI is assembled from whatever plugins are
discoverable, so adding a feature never means editing this file. What does live
here is the handful of commands that are about the workspace itself rather than
about repos — `init`, `check`, and `doctor`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from . import __version__
from .api import Context
from .plugins import all_plugins
from .workspace import CONFIG_NAME, Workspace


@click.group()
@click.version_option(__version__, prog_name="fonds")
def cli():
    """Manage a metarepo: a workspace of many git repos.

    Run inside a directory containing fonds.toml, or any subdirectory of one.
    """


def _register_plugin_commands() -> None:
    root = Workspace.find_root()
    for plugin in all_plugins(root):
        for command in plugin.commands:
            cli.add_command(command)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

GITIGNORE = """\
# Checkouts and generated artifacts are rebuildable; the inventory is not.
repos/
wikis/
tags/
data/
local/
.github-token
.venv/
__pycache__/
.DS_Store
"""

README = """\
# {owner} metarepo

A [fonds](https://github.com/jcushman/fonds) workspace: every repo belonging to
`{owner}`, checked out in one place, with an inventory of what they are.

    fonds clone        # clone or update every repo into repos/
    fonds inventory    # rebuild inventory/all.md
    fonds check        # assert the things that should stay true
    fonds --help       # everything else

`inventory/all.md` is the map. It is generated, but safe to edit: extra columns
and notes around the table survive regeneration, and `fonds tags sync` pushes
the Tags column back to GitHub.
"""

AGENTS = """\
This is a multi-repo workspace for `{owner}`, managed with
[fonds](https://github.com/jcushman/fonds).

Assume questions about code refer to the repos checked out in `repos/`.

`inventory/all.md` lists every repo with its description, tags, languages, and
activity — read it first when you need to know which repo something lives in.

`fonds --help` lists the available tooling.
"""

WORKFLOW = """\
name: Sync inventory

on:
  schedule:
    - cron: "0 6 * * *"
  workflow_dispatch:

permissions:
  contents: write

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5

      - uses: astral-sh/setup-uv@v8

      - name: Rebuild inventory
        env:
          GITHUB_TOKEN: ${{ secrets.INVENTORY_TOKEN || secrets.GITHUB_TOKEN }}
        run: |
          uvx --from git+https://github.com/jcushman/fonds fonds inventory --prune --all-tags
          uvx --from git+https://github.com/jcushman/fonds fonds check

      - name: Commit if changed
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add inventory/
          if git diff --staged --quiet; then
            exit 0
          fi
          # Don't commit runs where only "Last Updated" dates moved.
          trivial=true
          for f in $(git diff --staged --name-only -- inventory/); do
            if ! diff -q \\
                <(git show HEAD:"$f" 2>/dev/null | sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}/DATE/g') \\
                <(sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}/DATE/g' "$f") \\
                >/dev/null; then
              trivial=false
              break
            fi
          done
          if $trivial; then
            git reset
            exit 0
          fi
          git commit -m "Update inventory"
          git push
"""


@cli.command()
@click.argument("owner")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory to initialize. Defaults to the current directory.",
)
@click.option(
    "--workflow/--no-workflow",
    default=True,
    show_default=True,
    help="Write a GitHub Actions workflow that rebuilds the inventory nightly.",
)
def init(owner: str, path: Path | None, workflow: bool):
    """Create a workspace for OWNER, a GitHub user or organization."""
    root = (path or Path.cwd()).resolve()
    root.mkdir(parents=True, exist_ok=True)

    config = root / CONFIG_NAME
    if config.exists():
        raise click.ClickException(f"{config} already exists.")

    written = []

    def write(relative: str, content: str) -> None:
        target = root / relative
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        written.append(relative)

    write(CONFIG_NAME, f'owner = "{owner}"\n')
    write(".gitignore", GITIGNORE)
    write("README.md", README.format(owner=owner))
    write("AGENTS.md", AGENTS.format(owner=owner))
    if workflow:
        write(".github/workflows/sync-inventory.yml", WORKFLOW)

    for directory in ("repos", "wikis", "inventory", "data", "local"):
        (root / directory).mkdir(exist_ok=True)

    click.echo(f"Initialized a fonds workspace for {owner} in {root}\n")
    for relative in written:
        click.echo(f"  created {relative}")
    click.echo("\nNext:  fonds inventory   then   fonds clone")


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--offline",
    is_flag=True,
    help="Use the saved inventory instead of re-fetching from the source.",
)
def check(offline: bool):
    """Run every plugin's assertions about the workspace.

    Exits non-zero on the first failure, so a scheduled job can depend on it.
    """
    from .sources import Detail

    workspace = Workspace.find()
    checks = [
        (plugin, check)
        for plugin in all_plugins(workspace.root)
        for check in plugin.checks
    ]
    if not checks:
        click.echo("No checks registered.")
        return

    if offline:
        repos = _repos_from_inventory(workspace)
        click.echo(f"Checking {len(repos)} repos from {workspace.inventory_path}\n")
    else:
        repos = []
        for source in workspace.sources:
            repos.extend(source.list_repos(Detail.FULL))
        click.echo(f"Checking {len(repos)} repos\n")

    ctx = Context(workspace=workspace, repos=repos)
    failures = 0
    for plugin, item in checks:
        result = item.run(ctx)
        mark = "ok  " if result.ok else "FAIL"
        click.echo(f"  [{mark}] {plugin.name}/{item.name}: {result.message}")
        for detail in result.details:
            click.echo(f"           {detail}")
        if not result.ok:
            failures += 1

    click.echo()
    if failures:
        raise click.ClickException(f"{failures} check(s) failed.")
    click.echo(f"All {len(checks)} checks passed.")


def _repos_from_inventory(workspace: Workspace) -> list:
    """Reconstruct enough of each repo from the inventory to run checks offline."""
    from .repo import Repo
    from .table import read_table, row_tags

    path = workspace.inventory_path
    if not path.exists():
        raise click.ClickException(f"No inventory at {path}. Run `fonds inventory` first.")

    repos = []
    for row in read_table(path):
        if not row.get("Name"):
            continue
        repos.append(
            Repo(
                name=row["Name"],
                owner=workspace.config.sources[0].owner,
                default_branch=row.get("Default Branch", ""),
                archived=row.get("Archived") == "Yes",
                private=row.get("Visibility") == "Private",
                has_pages=row.get("GitHub Pages") == "Yes",
                tags=row_tags(row),
            )
        )
    return repos


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

@cli.command()
def doctor():
    """Report the workspace's configuration and any missing external tools."""
    root = Workspace.find_root()
    if root is None:
        click.echo(f"workspace:  none found (no {CONFIG_NAME} here or above)")
        click.echo("            run `fonds init <owner>` to create one")
    else:
        workspace = Workspace.load(root)
        click.echo(f"workspace:  {root}")
        for source in workspace.sources:
            filters = []
            if source.config.include:
                filters.append(f"include={list(source.config.include)}")
            if source.config.exclude:
                filters.append(f"exclude={list(source.config.exclude)}")
            click.echo(f"source:     {source.key} ({' '.join(filters) or 'all repos'})")

    from .sources.github import get_token

    click.echo(f"github auth: {'found' if get_token() else 'MISSING (run `gh auth login`)'}")

    click.echo("\nplugins:")
    for plugin in all_plugins(root):
        bits = []
        if plugin.commands:
            bits.append(f"{len(plugin.commands)} command(s)")
        if plugin.columns:
            bits.append(f"{len(plugin.columns)} column(s)")
        if plugin.checks:
            bits.append(f"{len(plugin.checks)} check(s)")
        click.echo(f"  {plugin.name:12s} {', '.join(bits)}")

    requirements = [
        (plugin, requirement)
        for plugin in all_plugins(root)
        for requirement in plugin.requires
    ]
    if requirements:
        click.echo("\nexternal tools:")
        for plugin, requirement in requirements:
            found = requirement.find()
            if found:
                click.echo(f"  {requirement.binary:12s} {found}")
            else:
                click.echo(
                    f"  {requirement.binary:12s} MISSING — {requirement.install} "
                    f"(needed by {requirement.used_by or plugin.name})"
                )


def main() -> int:
    _register_plugin_commands()
    return cli(standalone_mode=True)


if __name__ == "__main__":
    sys.exit(main())
