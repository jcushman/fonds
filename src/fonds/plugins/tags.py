"""Reading and writing repo tags.

Tags are the workspace's own vocabulary — "everything to do with Perma",
"things I still maintain" — and they are what `--tag` filters on everywhere
else. The storage backend differs between orgs and user accounts; see
`fonds/tags.py`. Nothing here needs to know which one is in play.
"""

from __future__ import annotations

from pathlib import Path

import click

from ..api import Plugin
from ..selection import Selection, repo_options
from ..table import read_table
from ..tags import parse_tags
from ..workspace import Workspace


@click.group("tags")
def tags_group():
    """Read and write repo tags."""


@tags_group.command("list")
@repo_options
@click.option("--local-only", is_flag=True, help="Only show repos checked out here.")
def list_tags(selection: Selection, local_only: bool):
    """List tags and the repos carrying them."""
    from ..tags import build_tag_store

    store = build_tag_store(selection.workspace)
    all_tags = store.get_all()
    if selection.repo_names:
        wanted = set(selection.repo_names)
        all_tags = {name: tags for name, tags in all_tags.items() if name in wanted}
    if local_only:
        present = {p.name for p in selection.workspace.repos_dir.glob("*") if p.is_dir()}
        all_tags = {name: tags for name, tags in all_tags.items() if name in present}

    # Seed with the known vocabulary so tags with no repos are still visible.
    by_tag: dict[str, list[str]] = {
        tag: [] for tag in store.allowed() if not selection.tag or tag == selection.tag
    }
    for name, repo_tags in all_tags.items():
        for tag in repo_tags:
            if selection.tag and tag != selection.tag:
                continue
            by_tag.setdefault(tag, []).append(name)

    if not by_tag:
        click.echo("No tags found.")
        return
    for tag in sorted(by_tag):
        click.echo(f"{tag}:")
        for name in sorted(by_tag[tag]):
            click.echo(f"  {name}")


def _edit(workspace: Workspace, tag: str, repos: tuple[str, ...], add: bool) -> None:
    from ..tags import build_tag_store

    store = build_tag_store(workspace)
    current = store.get_all()
    verb = "added" if add else "removed"

    for name in repos:
        if name not in current:
            click.echo(f"{name}: not found at the source", err=True)
            continue
        existing = set(current[name])
        if add and tag in existing:
            click.echo(f"{name}: already has {tag!r}")
            continue
        if not add and tag not in existing:
            click.echo(f"{name}: does not have {tag!r}")
            continue
        updated = sorted(existing | {tag}) if add else sorted(existing - {tag})
        store.set(name, updated)
        click.echo(f"{name}: {verb} {tag!r}")


@tags_group.command("add")
@click.argument("tag")
@click.argument("repos", nargs=-1, required=True)
def add_tag(tag: str, repos: tuple[str, ...]):
    """Add TAG to one or more repos."""
    _edit(Workspace.find(), tag, repos, add=True)


@tags_group.command("remove")
@click.argument("tag")
@click.argument("repos", nargs=-1, required=True)
def remove_tag(tag: str, repos: tuple[str, ...]):
    """Remove TAG from one or more repos."""
    _edit(Workspace.find(), tag, repos, add=False)


@tags_group.command("sync")
@click.argument("inventory_file", type=click.Path(exists=True, path_type=Path), required=False)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
def sync_tags(inventory_file: Path | None, yes: bool):
    """Push the inventory's Tags column back to the source.

    Edit the Tags column in inventory/all.md, then run this — it is a much
    better editing surface than one command per repo.
    """
    from ..tags import build_tag_store

    workspace = Workspace.find()
    path = inventory_file or workspace.inventory_path
    if not path.exists():
        raise click.ClickException(f"No inventory at {path}. Run `fonds inventory` first.")

    rows = read_table(path)
    if not rows:
        raise click.ClickException(f"No rows found in {path}.")
    if "Tags" not in rows[0]:
        raise click.ClickException(f"{path} has no 'Tags' column.")

    desired = {
        row["Name"].strip(): parse_tags(row.get("Tags", ""))
        for row in rows
        if row.get("Name", "").strip()
    }
    click.echo(f"Read {len(desired)} repos from {path}")
    click.echo("Fetching current tags ...")

    store = build_tag_store(workspace)
    current = store.get_all()

    missing = sorted(name for name in desired if name not in current)
    if missing:
        click.echo(f"\n{len(missing)} repo(s) in the inventory not found at the source (skipping):")
        for name in missing:
            click.echo(f"  {name}")

    changes: dict[str, tuple[list[str], list[str]]] = {}
    for name, want in desired.items():
        if name not in current:
            continue
        have = set(current[name])
        to_add = sorted(set(want) - have)
        to_remove = sorted(have - set(want))
        if to_add or to_remove:
            changes[name] = (to_add, to_remove)

    if not changes:
        click.echo("\nAll tags are already in sync.")
        return

    click.echo()
    for name in sorted(changes):
        to_add, to_remove = changes[name]
        bits = []
        if to_add:
            bits.append(f"+{', +'.join(to_add)}")
        if to_remove:
            bits.append(f"-{', -'.join(to_remove)}")
        click.echo(f"  {name}: {'  '.join(bits)}")
    click.echo(f"\n{len(changes)} repo(s) will be updated.")

    if not yes and not click.confirm("Proceed?"):
        click.echo("Aborted.")
        return

    for index, name in enumerate(sorted(changes), start=1):
        click.echo(f"  [{index}/{len(changes)}] {name}: {', '.join(desired[name]) or '(none)'}")
        store.set(name, desired[name])
    click.echo(f"\nDone. Updated {len(changes)} repo(s).")


PLUGIN = Plugin(
    name="tags",
    help="Read and write repo tags.",
    commands=(tags_group,),
)
