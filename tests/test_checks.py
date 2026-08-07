"""Guards are the reason a nightly job is worth running; they need to fail."""

from __future__ import annotations


from fonds.api import Context
from fonds.plugins.guards import _check_private_pages
from fonds.repo import Repo
from fonds.workspace import Workspace


def workspace_with(tmp_path, config: str) -> Workspace:
    (tmp_path / "fonds.toml").write_text(config)
    return Workspace.load(tmp_path)


def repos(*specs) -> list[Repo]:
    return [
        Repo(name=name, owner="jcushman", private=private, has_pages=pages)
        for name, private, pages in specs
    ]


def test_private_repo_with_pages_fails(tmp_path):
    ctx = Context(
        workspace=workspace_with(tmp_path, 'owner = "jcushman"\n'),
        repos=repos(("leaky", True, True), ("fine", False, True)),
    )
    result = _check_private_pages(ctx)
    assert not result.ok
    assert list(result.details) == ["leaky"]


def test_allowlisted_private_pages_passes(tmp_path):
    ctx = Context(
        workspace=workspace_with(
            tmp_path,
            'owner = "jcushman"\n\n[guards]\nprivate_pages_allowlist = ["leaky"]\n',
        ),
        repos=repos(("leaky", True, True)),
    )
    assert _check_private_pages(ctx).ok


def test_allowlist_accepts_globs(tmp_path):
    ctx = Context(
        workspace=workspace_with(
            tmp_path,
            'owner = "jcushman"\n\n[guards]\nprivate_pages_allowlist = ["*-showcase"]\n',
        ),
        repos=repos(("demo-showcase", True, True), ("other", True, True)),
    )
    result = _check_private_pages(ctx)
    assert not result.ok
    assert list(result.details) == ["other"]


def test_public_repo_with_pages_is_fine(tmp_path):
    ctx = Context(
        workspace=workspace_with(tmp_path, 'owner = "jcushman"\n'),
        repos=repos(("site", False, True)),
    )
    assert _check_private_pages(ctx).ok
