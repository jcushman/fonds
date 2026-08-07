"""Assertions about the workspace that a scheduled job can fail on.

An inventory that is only ever read by people drifts quietly. These turn some
of what it records into things that break loudly instead — the reason to run
`fonds inventory && fonds check` on a schedule rather than just the former.
"""

from __future__ import annotations

from fnmatch import fnmatch

from ..api import Check, CheckResult, Context, Plugin, failed, ok


def _check_private_pages(ctx: Context) -> CheckResult:
    """A private repo publishing a GitHub Pages site is publishing its build.

    Pages sites built from private repos are world-readable unless the owner
    has deliberately put something in front of them, so this fails closed: new
    ones must be acknowledged in fonds.toml before they stop being an alarm.

        [guards]
        private_pages_allowlist = ["docs-site", "*-showcase"]
    """
    allowlist = ctx.settings("guards").get("private_pages_allowlist", [])
    allowed = lambda name: any(fnmatch(name, pattern) for pattern in allowlist)  # noqa: E731
    offenders = sorted(
        repo.name
        for repo in ctx.repos
        if repo.private and repo.has_pages and not allowed(repo.name)
    )
    if offenders:
        return failed(
            f"{len(offenders)} private repo(s) have GitHub Pages enabled and are "
            "not in guards.private_pages_allowlist",
            offenders,
        )
    return ok("no unexpected Pages sites on private repos")


def _check_archived_not_default(ctx: Context) -> CheckResult:
    """Archived repos with no default branch are usually broken imports."""
    offenders = sorted(
        repo.name for repo in ctx.repos if not repo.archived and not repo.default_branch
    )
    if offenders:
        return failed(
            f"{len(offenders)} active repo(s) have no default branch", offenders
        )
    return ok("every active repo has a default branch")


PLUGIN = Plugin(
    name="guards",
    help="Assertions run by `fonds check`.",
    checks=(
        Check(
            name="private-pages",
            run=_check_private_pages,
            help="No private repo serves GitHub Pages without being allowlisted.",
        ),
        Check(
            name="default-branch",
            run=_check_archived_not_default,
            help="Every active repo has a default branch.",
        ),
    ),
)
