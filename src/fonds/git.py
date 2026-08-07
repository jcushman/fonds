"""Git plumbing shared by every command that touches a checkout."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

import click

# `--depth`/`--single-branch` clones pin the fetch refspec to one branch;
# `hydrate` widens it back to this.
ALL_BRANCHES_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"


def _run(args: list[str], cwd: Path | None = None, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, env=auth_env(), **kwargs
    )


def auth_env() -> dict[str, str]:
    """Environment for git subprocesses, with a token injected if we have one."""
    from .sources.github import get_token

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    token = get_token()
    if token:
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {auth}"
    return env


def is_dirty(path: Path) -> bool:
    """True if the working tree has uncommitted changes or unpushed commits."""
    status = _run(["git", "status", "--porcelain"], cwd=path)
    if status.returncode != 0 or status.stdout.strip():
        return True
    # No upstream, or commits ahead of it, both count as "don't touch this".
    log = _run(["git", "log", "--oneline", "@{upstream}..HEAD"], cwd=path)
    return log.returncode != 0 or bool(log.stdout.strip())


def local_head(path: Path) -> str | None:
    result = _run(["git", "rev-parse", "HEAD"], cwd=path)
    return result.stdout.strip() if result.returncode == 0 else None


def current_branch(path: Path) -> str | None:
    """Checked-out branch name, or None if detached or unknown."""
    result = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return None if not branch or branch == "HEAD" else branch


def is_shallow(path: Path) -> bool:
    return _run(["git", "rev-parse", "--is-shallow-repository"], cwd=path).stdout.strip() == "true"


def remote_has_refs(clone_url: str) -> bool | None:
    """True if the remote exists and has a HEAD, False if absent, None if unknown.

    None means the check itself failed, so callers can keep cached data rather
    than recording a transient network error as "no wiki".
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", clone_url, "HEAD"],
            capture_output=True,
            text=True,
            timeout=20,
            env=auth_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if result.returncode == 0 and result.stdout.strip():
        return True
    stderr = result.stderr.lower()
    if result.returncode in {2, 128} and (
        "not found" in stderr or "could not read from remote repository" in stderr
    ):
        return False
    return None


def checkout_dirs(dest: Path) -> set[str]:
    """Names of directories under *dest* that look like git checkouts."""
    if not dest.exists():
        return set()
    return {p.name for p in dest.iterdir() if p.is_dir() and (p / ".git").exists()}


def clone_or_update(
    name: str,
    clone_url: str,
    dest: Path,
    remote_head: str | None = None,
    default_branch: str | None = None,
) -> str:
    """Clone or update one checkout.

    Returns "cloned", "updated", "current", "dirty", "drift", or "failed".
    """
    path = dest / name

    if path.is_dir():
        if is_dirty(path):
            click.echo(f"  {name}: dirty, skipping")
            return "dirty"
        # A single-branch clone keeps following the branch it was cloned at. If
        # the default branch has since moved, pulling would silently track the
        # stale one — flag it for `hydrate` instead of quietly diverging.
        branch = current_branch(path)
        if default_branch and branch and branch != default_branch:
            click.echo(
                f"  {name}: on '{branch}' but default is '{default_branch}'; "
                f"run `fonds hydrate` — skipping"
            )
            return "drift"
        if remote_head and local_head(path) == remote_head:
            click.echo(f"  {name}: current, skipping")
            return "current"
        click.echo(f"  {name}: updating ...")
        result = _run(["git", "pull", "--ff-only"], cwd=path)
        if result.returncode != 0:
            click.echo(f"  {name}: pull failed: {result.stderr.strip()[:200]}", err=True)
            return "failed"
        return "updated"

    click.echo(f"  {name}: cloning ...")
    result = _run(["git", "clone", "--depth=1", clone_url, str(path)])
    if result.returncode != 0:
        click.echo(f"  {name}: clone failed: {result.stderr.strip()[:200]}", err=True)
        return "failed"
    return "cloned"


def hydrate_checkout(
    name: str,
    path: Path,
    default_branch: str | None,
    unshallow: bool,
    checkout_default: bool,
) -> str:
    """Widen one shallow/single-branch checkout. Returns "hydrated" or "failed"."""
    steps = [["git", "config", "remote.origin.fetch", ALL_BRANCHES_REFSPEC]]
    fetch = ["git", "fetch", "origin"]
    if unshallow and is_shallow(path):
        fetch.append("--unshallow")
    steps.append(fetch)
    # Repair origin/HEAD so it points at the *current* default branch.
    steps.append(["git", "remote", "set-head", "origin", "-a"])

    for step in steps:
        result = _run(step, cwd=path)
        if result.returncode != 0:
            click.echo(f"  {name}: hydrate failed: {result.stderr.strip()[:200]}", err=True)
            return "failed"

    if checkout_default and default_branch and current_branch(path) != default_branch:
        if is_dirty(path):
            click.echo(f"  {name}: dirty, staying put (default is '{default_branch}')")
        else:
            result = _run(["git", "checkout", default_branch], cwd=path)
            if result.returncode != 0:
                click.echo(f"  {name}: checkout failed", err=True)
                return "failed"
    return "hydrated"


def delete_checkouts(dest: Path, names: list[str]) -> int:
    """Delete the named checkouts under *dest*, keeping any that are dirty."""
    deleted = 0
    for name in names:
        path = dest / name
        if is_dirty(path):
            click.echo(f"  {name}: dirty, keeping")
        else:
            shutil.rmtree(path)
            click.echo(f"  {name}: deleted")
            deleted += 1
    return deleted
