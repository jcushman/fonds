"""The inventory: one table describing every repo in the workspace.

This is the artifact the rest of the workspace hangs off. `clone` reads default
branches from it, `tags sync` writes back from it, `--tag` filters resolve
against it offline, and a human can add columns and notes to it without those
being clobbered on the next run.

Columns are contributed by plugins, so a workspace's table can say more than
this file knows about.
"""

from __future__ import annotations

from pathlib import Path

import click

from ..api import Column, Context, Plugin
from ..git import remote_has_refs
from ..repo import Repo
from ..selection import Selection, repo_options
from ..sources import Detail
from ..table import FORMATS, Format, row_tags
from ..workspace import Workspace

LAST_UPDATED = "Last Updated"


# ---------------------------------------------------------------------------
# Core columns
# ---------------------------------------------------------------------------

def _languages(repo: Repo, ctx: Context) -> str:
    total = sum(repo.languages.values())
    if not total:
        return ""
    return ", ".join(
        f"{language} {round(size / total * 100)}%"
        for language, size in sorted(repo.languages.items(), key=lambda item: -item[1])
    )


def _wiki_url(repo: Repo, ctx: Context) -> str:
    """Link to the wiki only if it is enabled *and* has content."""
    cached = ctx.existing.get("Wiki URL", "")
    if not repo.has_wiki:
        return ""
    has_refs = remote_has_refs(repo.wiki_clone_url)
    if has_refs is True:
        return f"{repo.html_url}/wiki"
    if has_refs is False:
        return ""
    return cached  # the check failed; keep whatever we knew before


def _contributors(repo: Repo, ctx: Context) -> str:
    source = ctx.source_for(repo)
    if not hasattr(source, "contributors"):
        return ""
    return ", ".join(source.contributors(repo))


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


COLUMNS: tuple[Column, ...] = (
    Column("Name", lambda r, c: r.name, width=22),
    Column("Description", lambda r, c: r.description, width=40),
    Column("URL", lambda r, c: r.html_url, width=45),
    Column("Wiki URL", _wiki_url, slow=True, width=45),
    Column("GitHub Pages", lambda r, c: _yes_no(r.has_pages), width=14, center=True),
    Column("Default Branch", lambda r, c: r.default_branch, width=18),
    Column("Other Branches", lambda r, c: r.other_branches, width=25),
    Column("Visibility", lambda r, c: "Private" if r.private else "Public", width=12, center=True),
    Column("Fork", lambda r, c: _yes_no(r.fork), width=8, center=True),
    Column("Archived", lambda r, c: _yes_no(r.archived), width=10, center=True),
    Column("Stars", lambda r, c: r.stars, width=8, center=True),
    Column("Forks", lambda r, c: r.forks, width=8, center=True),
    Column(
        LAST_UPDATED,
        lambda r, c: r.updated_at.strftime("%Y-%m-%d") if r.updated_at else "",
        width=14,
        center=True,
    ),
    Column("Contributors", _contributors, slow=True, width=40),
    Column("Languages", _languages, width=35),
    Column("Tags", lambda r, c: ", ".join(r.tags), width=25),
)


def all_columns(workspace: Workspace | None = None) -> list[Column]:
    """Every column, core plus plugin-contributed."""
    from . import all_plugins

    root = workspace.root if workspace else None
    columns: list[Column] = []
    seen: set[str] = set()
    for plugin in all_plugins(root):
        for column in plugin.columns:
            if column.name not in seen:
                seen.add(column.name)
                columns.append(column)
    return columns


def column_style(name: str) -> tuple[int, bool]:
    """(width, centered) for a column, for Excel output."""
    for column in all_columns():
        if column.name == name:
            return column.width, column.center
    return 20, False


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------

def _is_cached(repo: Repo, existing: dict, slow: list[str]) -> bool:
    """True if *existing* was written for this exact revision of the repo."""
    if not existing:
        return False
    date = repo.updated_at.strftime("%Y-%m-%d") if repo.updated_at else ""
    return existing.get(LAST_UPDATED) == date and all(name in existing for name in slow)


def build_row(repo: Repo, ctx: Context, columns: list[Column]) -> tuple[dict, bool]:
    """Render one row. Returns (row, used_cache)."""
    slow_names = [column.name for column in columns if column.slow]
    cached = _is_cached(repo, ctx.existing, slow_names)
    row = {}
    for column in columns:
        if cached and column.slow:
            row[column.name] = ctx.existing[column.name]
        else:
            row[column.name] = column.value(repo, ctx)
    return row, cached


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _write(
    fmt: Format, path: Path, rows: list[dict], columns: list[str], prune: bool
) -> None:
    if path.exists():
        stale = fmt.update(path, rows, columns, prune)
        for name in stale:
            action = "pruned" if prune else "no longer in source (pass --prune to remove)"
            click.echo(f"  {name}: {action}")
    else:
        fmt.write(rows, path, columns)
    click.echo(f"Saved {len(rows)} repos to {path}")


def _write_tag_files(
    fmt: Format,
    out_dir: Path,
    rows: list[dict],
    columns: list[str],
    prune: bool,
    known_tags: list[str],
) -> None:
    by_tag: dict[str, list[dict]] = {}
    for row in rows:
        for tag in row_tags(row):
            by_tag.setdefault(tag, []).append(row)

    for tag in sorted(by_tag):
        _write(fmt, out_dir / f"{tag}{fmt.ext}", by_tag[tag], columns, prune)

    for tag in sorted(set(known_tags) - set(by_tag)):
        path = out_dir / f"{tag}{fmt.ext}"
        if not path.exists():
            continue
        if prune:
            path.unlink()
            click.echo(f"Removed {path}: tag has no repos")
        else:
            click.echo(f"Skipping {path}: tag has no repos")


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

@click.command()
@repo_options
@click.option(
    "--format",
    "format_name",
    type=click.Choice(list(FORMATS), case_sensitive=False),
    default="markdown",
    show_default=True,
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Output file, or directory with --all-tags. Defaults to inventory/.",
)
@click.option("--prune", is_flag=True, help="Drop rows for repos that no longer exist.")
@click.option(
    "--all-tags",
    is_flag=True,
    help="Also write one file per tag alongside the main inventory.",
)
def inventory(
    selection: Selection,
    format_name: str,
    output: Path | None,
    prune: bool,
    all_tags: bool,
):
    """Export an inventory of every repo in the workspace."""
    if all_tags and selection.tag:
        raise click.UsageError("--all-tags and --tag are mutually exclusive.")

    workspace = selection.workspace
    fmt = FORMATS[format_name]

    if all_tags:
        out_dir = output or workspace.inventory_dir
        if out_dir.exists() and not out_dir.is_dir():
            raise click.ClickException(f"{out_dir} exists and is not a directory.")
        out_path = out_dir / f"all{fmt.ext}"
    else:
        out_path = output or workspace.inventory_dir / f"all{fmt.ext}"

    # Seed from the previous run so slow columns can be reused.
    existing: dict[str, dict] = {}
    if out_path.exists():
        existing = {row["Name"]: row for row in fmt.read(out_path)}

    click.echo(f"Fetching repos for {', '.join(s.key for s in selection.sources)} ...")
    repos = selection.remote(Detail.FULL)
    click.echo(f"Found {len(repos)} repos. Filling in per-repo detail ...\n")

    ctx = Context(workspace=workspace, repos=repos)
    columns = all_columns(workspace)
    names = [column.name for column in columns]

    rows = []
    fetched = cached_count = 0
    for index, repo in enumerate(repos, start=1):
        ctx.existing = existing.get(repo.name, {})
        row, was_cached = build_row(repo, ctx, columns)
        rows.append(row)
        click.echo(f"  [{index}/{len(repos)}] {repo.full_name}{' (cached)' if was_cached else ''}")
        if was_cached:
            cached_count += 1
        else:
            fetched += 1

    click.echo(f"\n{fetched} fetched, {cached_count} cached.\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write(fmt, out_path, rows, names, prune)

    if all_tags:
        _write_tag_files(fmt, out_dir, rows, names, prune, ctx.tags.allowed())


PLUGIN = Plugin(
    name="inventory",
    help="Export a table of every repo in the workspace.",
    commands=(inventory,),
    columns=COLUMNS,
)
