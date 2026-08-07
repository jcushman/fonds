"""Getting every repo onto disk, and keeping it there.

Clones are shallow and single-branch by default, which is what makes a
few-hundred-repo workspace practical to keep current. `hydrate` widens
individual repos when you actually need history or another branch.

Nothing here ever touches a checkout with uncommitted or unpushed work.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import click

from ..api import Plugin
from ..git import checkout_dirs, clone_or_update, delete_checkouts, hydrate_checkout
from ..selection import Selection, repo_options
from ..table import read_table, row_tags


def _wiki_targets(selection: Selection) -> list[tuple[str, str]]:
    """(name, clone_url) for wikis known to have content, read from the inventory.

    The inventory already recorded which wikis exist, so this costs nothing;
    without one we simply skip wikis rather than probing every repo.
    """
    path = selection.workspace.inventory_path
    if not path.exists():
        click.echo(f"No inventory at {path}; skipping wikis.")
        return []

    rows = read_table(path)
    if rows and "Wiki URL" not in rows[0]:
        click.echo(f"{path} has no 'Wiki URL' column; skipping wikis.")
        return []

    wanted = set(selection.repo_names)
    targets = []
    for row in rows:
        name, wiki_url = row.get("Name", ""), row.get("Wiki URL", "")
        if not name or not wiki_url:
            continue
        if wanted and name not in wanted:
            continue
        if selection.tag and selection.tag not in row_tags(row):
            continue
        base = wiki_url.rstrip("/").removesuffix("/wiki").removesuffix(".git")
        targets.append((name, f"{base}.wiki.git"))
    return sorted(targets, key=lambda target: target[0].lower())


def _wiki_dest(selection: Selection, output_dir: Path | None, repo_dest: Path) -> Path:
    if output_dir:
        return repo_dest.parent / f"{repo_dest.name}-wikis"
    if selection.tag:
        return selection.workspace.wiki_tag_dir(selection.tag)
    return selection.workspace.wikis_dir


def _report_local_only(dest: Path, expected: set[str], label: str, delete: bool) -> tuple[int, int]:
    """Report checkouts on disk that the source no longer lists."""
    local_only = sorted(checkout_dirs(dest) - expected)
    if not local_only:
        return 0, 0
    click.echo(f"\n{len(local_only)} local-only {label}(s) (not found in source):")
    for name in local_only:
        click.echo(f"  {name}")
    deleted = delete_checkouts(dest, local_only) if delete else 0
    return len(local_only), deleted


@click.command()
@repo_options
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to clone. Defaults to tags/<tag>/ with --tag, otherwise repos/.",
)
@click.option(
    "--delete-local-only",
    is_flag=True,
    help="Delete clean checkouts that no longer exist at the source.",
)
@click.option("--wikis/--no-wikis", default=True, show_default=True, help="Also clone wikis.")
@click.option(
    "--wiki-output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to clone wikis. Defaults to wikis/ or wikis/<tag>/.",
)
def clone(
    selection: Selection,
    output_dir: Path | None,
    delete_local_only: bool,
    wikis: bool,
    wiki_output_dir: Path | None,
):
    """Clone every repo in the workspace, or update existing checkouts."""
    dest = selection.checkout_root(output_dir)
    dest.mkdir(parents=True, exist_ok=True)

    repos = selection.remote()
    click.echo(f"Found {len(repos)} repos")

    default_branches = selection.default_branches()
    counts: Counter[str] = Counter()
    for repo in repos:
        counts[
            clone_or_update(
                repo.dir_name,
                repo.clone_url,
                dest,
                repo.default_branch_oid,
                default_branches.get(repo.name),
            )
        ] += 1

    local_only, deleted = _report_local_only(
        dest, {repo.dir_name for repo in repos}, "repo", delete_local_only
    )

    wiki_counts: Counter[str] = Counter()
    wiki_local_only = wiki_deleted = 0
    if wikis:
        targets = _wiki_targets(selection)
        if targets:
            wiki_dest = (wiki_output_dir or _wiki_dest(selection, output_dir, dest)).resolve()
            wiki_dest.mkdir(parents=True, exist_ok=True)
            click.echo(f"\nFound {len(targets)} wikis")
            for name, clone_url in targets:
                wiki_counts[clone_or_update(name, clone_url, wiki_dest)] += 1
            wiki_local_only, wiki_deleted = _report_local_only(
                wiki_dest, {name for name, _ in targets}, "wiki", delete_local_only
            )

    parts = [
        f"{counts['cloned']} cloned",
        f"{counts['updated']} updated",
        f"{counts['current']} current/skipped",
        f"{counts['dirty']} dirty/skipped",
    ]
    if counts["drift"]:
        parts.append(f"{counts['drift']} drifted (run `fonds hydrate`)")
    if counts["failed"]:
        parts.append(f"{counts['failed']} failed")
    if local_only:
        parts.append(f"{local_only} local-only")
    if deleted:
        parts.append(f"{deleted} deleted")
    if wikis and wiki_counts:
        parts.append(
            f"wikis: {wiki_counts['cloned']} cloned, {wiki_counts['updated']} updated, "
            f"{wiki_counts['current']} current"
        )
        if wiki_counts["failed"]:
            parts.append(f"{wiki_counts['failed']} wikis failed")
        if wiki_local_only:
            parts.append(f"{wiki_local_only} local-only wikis")
        if wiki_deleted:
            parts.append(f"{wiki_deleted} wikis deleted")
    click.echo("\nDone: " + ", ".join(parts))


@click.command()
@repo_options
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Where the checkouts are. Defaults to tags/<tag>/ with --tag, otherwise repos/.",
)
@click.option(
    "--unshallow/--no-unshallow",
    default=False,
    show_default=True,
    help="Also fetch full history (slower, more disk).",
)
@click.option(
    "--checkout-default/--no-checkout-default",
    default=True,
    show_default=True,
    help="Check out the current default branch after fetching.",
)
def hydrate(
    selection: Selection,
    output_dir: Path | None,
    unshallow: bool,
    checkout_default: bool,
):
    """Widen shallow, single-branch checkouts into full ones.

    `clone` produces checkouts that track only the branch that was default when
    they were cloned. This widens the fetch refspec to every branch, repairs
    origin/HEAD, and checks out the current default — fixing what `clone`
    reports as "drifted".
    """
    checkouts = selection.local(output_dir)
    default_branches = selection.default_branches()

    click.echo(f"Hydrating {len(checkouts)} repo(s)")
    counts: Counter[str] = Counter()
    for checkout in checkouts:
        click.echo(f"  {checkout.name}: hydrating ...")
        counts[
            hydrate_checkout(
                checkout.name,
                checkout.path,
                default_branches.get(checkout.name),
                unshallow,
                checkout_default,
            )
        ] += 1

    parts = [f"{counts['hydrated']} hydrated"]
    if counts["failed"]:
        parts.append(f"{counts['failed']} failed")
    click.echo("\nDone: " + ", ".join(parts))


PLUGIN = Plugin(
    name="clone",
    help="Clone and update the workspace's checkouts.",
    commands=(clone, hydrate),
)
