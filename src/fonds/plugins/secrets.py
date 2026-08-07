"""Scanning every checkout for leaked credentials, with trufflehog.

A small plugin on purpose: it contributes one command, one external-tool
requirement, one check, and one inventory column, without the rest of the tool
knowing it exists.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import click

from ..api import Check, CheckResult, Column, Context, Plugin, Requirement, failed, ok
from ..repo import Repo
from ..selection import Selection, repo_options

TRUFFLEHOG = Requirement(
    binary="trufflehog",
    install="brew install trufflehog",
    url="https://github.com/trufflesecurity/trufflehog",
    used_by="fonds secrets",
)


def _scan(path: Path) -> list[dict]:
    """Scan one checkout's full history. Findings are not verified against
    live services, so this never phones a credential out to a third party."""
    result = subprocess.run(
        [
            TRUFFLEHOG.resolve(),
            "git",
            f"file://{path}",
            "--no-update",
            "--no-verification",
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    return [
        json.loads(line) for line in result.stdout.strip().splitlines() if line.strip()
    ]


def _describe(finding: dict) -> tuple[str, str, str, str]:
    git = finding.get("SourceMetadata", {}).get("Data", {}).get("Git", {})
    return (
        finding.get("DetectorName", "unknown"),
        git.get("file", "?"),
        git.get("commit", "?")[:8],
        finding.get("Redacted", "?"),
    )


def _results_path(workspace, name: str) -> Path:
    return workspace.data_dir("secrets") / f"{name}.json"


@click.command()
@repo_options
@click.option(
    "--dir",
    "base_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory of checkouts to scan. Defaults to repos/.",
)
@click.option("--json-output", is_flag=True, help="Emit raw trufflehog JSON.")
@click.option(
    "--save/--no-save",
    default=True,
    show_default=True,
    help="Cache per-repo results under data/secrets/ for the inventory column.",
)
def secrets(selection: Selection, base_dir: Path | None, json_output: bool, save: bool):
    """Scan local checkouts for leaked secrets.

    Exits non-zero if anything is found, so it can gate a scheduled job.
    """
    TRUFFLEHOG.resolve()
    checkouts = selection.local(base_dir)
    if save:
        selection.workspace.data_dir("secrets", create=True)

    click.echo(f"Scanning {len(checkouts)} repo(s) for secrets ...\n")
    with_findings = total = 0

    for checkout in checkouts:
        findings = _scan(checkout.path)
        if save:
            _results_path(selection.workspace, checkout.name).write_text(
                json.dumps({"count": len(findings)}, indent=2) + "\n"
            )
        if not findings:
            click.echo(f"  {checkout.name}: clean")
            continue

        with_findings += 1
        total += len(findings)
        if json_output:
            for finding in findings:
                click.echo(json.dumps(finding))
            continue

        click.echo(f"  {checkout.name}: {len(findings)} finding(s)")
        seen = set()
        for finding in findings:
            detector, file, commit, redacted = _describe(finding)
            if (detector, file, redacted) in seen:
                continue
            seen.add((detector, file, redacted))
            click.echo(f"    [{detector}] {file} (commit {commit}) {redacted}")

    click.echo(
        f"\nDone: {len(checkouts)} scanned, {with_findings} with findings, "
        f"{total} total finding(s)"
    )
    if with_findings:
        raise SystemExit(1)


def _secrets_column(repo: Repo, ctx: Context) -> str:
    """Findings from the last `fonds secrets` run, if there was one."""
    path = _results_path(ctx.workspace, repo.dir_name)
    if not path.is_file():
        return ""
    count = json.loads(path.read_text()).get("count", 0)
    return str(count) if count else "clean"


def _check_no_secrets(ctx: Context) -> CheckResult:
    data_dir = ctx.workspace.data_dir("secrets")
    if not data_dir.is_dir():
        return ok("no scan results yet (run `fonds secrets`)")
    dirty = sorted(
        path.stem
        for path in data_dir.glob("*.json")
        if json.loads(path.read_text()).get("count", 0)
    )
    if dirty:
        return failed(f"{len(dirty)} repo(s) have secret-scan findings", dirty)
    return ok("no findings in the last scan")


PLUGIN = Plugin(
    name="secrets",
    help="Scan checkouts for leaked credentials.",
    commands=(secrets,),
    columns=(Column("Secrets", _secrets_column, width=12, center=True),),
    checks=(
        Check(
            name="secrets",
            run=_check_no_secrets,
            help="No repo has outstanding secret-scan findings.",
        ),
    ),
    requires=(TRUFFLEHOG,),
)
