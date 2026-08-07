"""Dependency analysis across the whole workspace.

Having every repo on disk at once makes questions possible that are awkward one
repo at a time: which of our own repos depend on each other, what do we all
share a version of, and what are we pulling from someone's personal account.

`scan` caches a CycloneDX BOM per repo under data/deps/; the reports read those
and never touch the network.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import click

from ..api import Column, Context, Plugin, Requirement
from ..repo import Repo
from ..selection import Selection, repo_options

CDXGEN = Requirement(
    binary="cdxgen",
    install="brew install cdxgen",
    url="https://github.com/CycloneDX/cdxgen",
    used_by="fonds deps scan",
)

# Actions are resolved by GitHub, not a package registry; they would swamp the
# external-dependency report with noise.
GITHUB_NATIVE_ECOSYSTEMS = {"githubactions"}
GITHUB_URL_RE = re.compile(r"github\.com[/:]([^/\s]+)/([^/.#@\s]+)")


def data_dir(workspace, create: bool = False) -> Path:
    return workspace.data_dir("deps", create=create)


def load_boms(directory: Path) -> dict[str, dict]:
    """{repo_name: bom} for every cached BOM."""
    return {
        path.stem: json.loads(path.read_text())
        for path in sorted(directory.glob("*.json"))
    }


def parse_purl(purl: str) -> tuple[str, str, str, str]:
    """(ecosystem, namespace, name, version) from a package URL."""
    without_scheme = purl.split(":", 1)[1]
    ecosystem, _, rest = without_scheme.partition("/")
    rest = unquote(rest)

    version = ""
    if "@" in rest:
        rest, version = rest.rsplit("@", 1)
    namespace, _, name = rest.rpartition("/")
    return ecosystem, namespace, name, version


def component_props(component: dict) -> dict[str, str]:
    return {p["name"]: p["value"] for p in component.get("properties", [])}


def is_github_action(component: dict) -> bool:
    return "cdx:github:action:uses" in component_props(component)


def relevant_components(bom: dict):
    """Components worth reporting on: real packages, not Actions."""
    for component in bom.get("components", []):
        purl = component.get("purl", "")
        if not purl:
            continue
        ecosystem, namespace, name, version = parse_purl(purl)
        if ecosystem in GITHUB_NATIVE_ECOSYSTEMS or is_github_action(component):
            continue
        yield component, ecosystem, namespace, name, version


@click.group("deps")
def deps_group():
    """Scan and analyse dependencies."""


@deps_group.command()
@repo_options
@click.option(
    "--dir",
    "base_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory of checkouts to scan. Defaults to repos/.",
)
def scan(selection: Selection, base_dir: Path | None):
    """Generate and cache a CycloneDX BOM for each checkout."""
    cdxgen = CDXGEN.resolve()
    checkouts = selection.local(base_dir)
    dest = data_dir(selection.workspace, create=True)

    click.echo(f"Scanning {len(checkouts)} repos with cdxgen ...\n")
    for index, checkout in enumerate(checkouts, start=1):
        out_file = dest / f"{checkout.name}.json"
        click.echo(f"  [{index}/{len(checkouts)}] {checkout.name} ... ", nl=False)
        result = subprocess.run(
            [cdxgen, "--no-install-deps", "-o", str(out_file), str(checkout.path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            click.echo(f"error (exit {result.returncode})")
            if result.stderr.strip():
                click.echo(f"    {result.stderr.strip()[:200]}")
            continue
        if out_file.is_file():
            count = len(json.loads(out_file.read_text()).get("components", []))
            click.echo(f"{count} components")
        else:
            click.echo("done (no output written)")

    click.echo(f"\nBOMs written to {dest}")


def _require_boms(workspace) -> dict[str, dict]:
    boms = load_boms(data_dir(workspace))
    if not boms:
        raise click.ClickException(
            f"No BOMs in {data_dir(workspace)}. Run `fonds deps scan` first."
        )
    return boms


@deps_group.command()
def intra():
    """Report which repos in the workspace depend on which others."""
    from ..workspace import Workspace

    workspace = Workspace.find()
    boms = _require_boms(workspace)
    owners = {source.owner.lower() for source in workspace.sources}
    known = {name.lower() for name in boms}

    edges: dict[str, set[str]] = defaultdict(set)
    for repo_name, bom in boms.items():
        for _component, _eco, namespace, name, _version in relevant_components(bom):
            if namespace.lower().lstrip("@") not in owners:
                continue
            if name.lower() != repo_name.lower() and name.lower() in known:
                edges[repo_name].add(name)

    if not edges:
        click.echo("No intra-workspace dependencies found.")
        return

    reverse: dict[str, set[str]] = defaultdict(set)
    for consumer, dependencies in edges.items():
        for dependency in dependencies:
            reverse[dependency].add(consumer)

    click.echo("=== What each repo depends on ===\n")
    for consumer in sorted(edges):
        click.echo(f"  {consumer}:")
        for dependency in sorted(edges[consumer]):
            click.echo(f"    -> {dependency}")

    click.echo("\n=== Who depends on each repo ===\n")
    for dependency in sorted(reverse):
        click.echo(f"  {dependency}:")
        for consumer in sorted(reverse[dependency]):
            click.echo(f"    <- {consumer}")


@deps_group.command()
@click.option("-n", "--limit", type=int, default=None, help="Show only the top N.")
@click.option(
    "--ecosystem",
    "ecosystems",
    multiple=True,
    help="Limit to specific ecosystem(s), e.g. pypi, npm. Repeatable.",
)
@click.option("-v", "--verbose", is_flag=True, help="Break down by version.")
def top(limit: int | None, ecosystems: tuple[str, ...], verbose: bool):
    """Rank dependencies by how many repos use them."""
    from ..workspace import Workspace

    boms = _require_boms(Workspace.find())
    wanted = {e.lower() for e in ecosystems}

    versions: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for repo_name, bom in boms.items():
        for _component, ecosystem, namespace, name, version in relevant_components(bom):
            if wanted and ecosystem.lower() not in wanted:
                continue
            qualified = f"{namespace}/{name}" if namespace else name
            versions[(ecosystem, qualified)][version or "(unknown)"].add(repo_name)

    ranked = sorted(
        versions.items(),
        key=lambda item: (-len({r for repos in item[1].values() for r in repos}), item[0]),
    )
    if limit is not None:
        ranked = ranked[:limit]
    if not ranked:
        click.echo("No dependencies found.")
        return

    click.echo("=== Top dependencies by repo count ===\n")
    for (ecosystem, name), by_version in ranked:
        repos = {repo for repos in by_version.values() for repo in repos}
        suffix = f"  ({len(by_version)} versions)" if len(by_version) > 1 else ""
        click.echo(f"  {name}  ({ecosystem})  — {len(repos)} repos{suffix}")
        if verbose:
            for version in sorted(by_version, key=lambda v: (-len(by_version[v]), v)):
                using = sorted(by_version[version])
                click.echo(f"    {version:30s}  {len(using):3d}  {', '.join(using)}")


@dataclass
class ExternalHit:
    repo: str
    manifest: Path
    dep_name: str
    url: str


def _external_hit(
    component: dict, ecosystem: str, namespace: str, name: str,
    repo_name: str, repo_dir: Path,
) -> tuple[str, str, ExternalHit] | None:
    props = component_props(component)

    if ecosystem == "github":
        url = f"github.com/{namespace}/{name}"
    else:
        url = next(
            (
                value
                for value in (
                    props.get("ResolvedUrl", ""),
                    props.get("cdx:pypi:versionSpecifiers", ""),
                )
                if GITHUB_URL_RE.search(value)
            ),
            "",
        )
        if not url and props.get("cdx:npm:isRegistryDependency") == "false":
            url = props.get("ResolvedUrl", "")

    match = GITHUB_URL_RE.search(url) if url else None
    if not match:
        return None

    src_file = props.get("SrcFile", "")
    if src_file:
        manifest = repo_dir / src_file.removeprefix(f"repos/{repo_name}/")
        # package.json links somewhere useful; package-lock.json does not.
        if manifest.name == "package-lock.json" and manifest.with_name("package.json").is_file():
            manifest = manifest.with_name("package.json")
    else:
        manifest = repo_dir

    return match.group(1), match.group(2), ExternalHit(repo_name, manifest, name, url)


def _find_line(path: Path, needle: str) -> int:
    try:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if needle in line:
                return number
    except OSError:
        pass
    return 0


@deps_group.command()
def external():
    """Report dependencies on GitHub repos outside the workspace's owners.

    Grouped by owner, which is what surfaces the ones living in an individual's
    personal account rather than somewhere the team controls.
    """
    from ..workspace import Workspace

    workspace = Workspace.find()
    boms = _require_boms(workspace)
    owners = {source.owner.lower() for source in workspace.sources}

    by_owner: dict[str, dict[str, list[ExternalHit]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for repo_name, bom in boms.items():
        repo_dir = workspace.repos_dir / repo_name
        for component, ecosystem, namespace, name, _version in relevant_components(bom):
            hit = _external_hit(component, ecosystem, namespace, name, repo_name, repo_dir)
            if hit and hit[0].lower() not in owners:
                by_owner[hit[0]][hit[1]].append(hit[2])

    if not by_owner:
        click.echo("No external GitHub dependencies found.")
        return

    click.echo("=== External GitHub dependencies by owner ===\n")
    for owner in sorted(by_owner, key=str.lower):
        repos = by_owner[owner]
        references = sum(len(hits) for hits in repos.values())
        click.echo(f"{owner}  ({len(repos)} repos, {references} references):")
        for gh_repo in sorted(repos):
            hits = repos[gh_repo]
            click.echo(f"  {gh_repo}  <- {', '.join(sorted({h.repo for h in hits}))}")
            for hit in sorted(hits, key=lambda h: (h.repo, str(h.manifest))):
                line = _find_line(hit.manifest, hit.dep_name) if hit.manifest.is_file() else 0
                click.echo(f"    {hit.manifest}{f':{line}' if line else ''}")
        click.echo()


def _dependency_count(repo: Repo, ctx: Context) -> str:
    """Component count from the cached BOM, if this repo has been scanned."""
    path = data_dir(ctx.workspace) / f"{repo.dir_name}.json"
    if not path.is_file():
        return ""
    return str(len(json.loads(path.read_text()).get("components", [])))


PLUGIN = Plugin(
    name="deps",
    help="Scan and analyse dependencies across the workspace.",
    commands=(deps_group,),
    columns=(Column("Dependencies", _dependency_count, width=14, center=True),),
    requires=(CDXGEN,),
)
