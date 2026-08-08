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


# ---------------------------------------------------------------------------
# Pending (not-yet-a-repo) directories
# ---------------------------------------------------------------------------

def test_pending_repos_are_reported_without_failing(tmp_path):
    """Starting a directory before pushing it is the normal workflow, so this
    reports but must not fail — yet it must never stay silent, since these are
    the only things in the workspace with no copy anywhere else."""
    from fonds.plugins.clone import _check_pending_repos

    ws = workspace_with(tmp_path, 'owner = "jcushman"\n')
    (tmp_path / "repos" / "real-repo" / ".git").mkdir(parents=True)
    (tmp_path / "repos" / "wordscape").mkdir()

    result = _check_pending_repos(Context(workspace=ws))
    assert result.ok
    assert "wordscape" in result.message
    assert "real-repo" not in result.message


def test_no_pending_dirs_when_all_are_checkouts(tmp_path):
    from fonds.plugins.clone import _check_pending_repos

    ws = workspace_with(tmp_path, 'owner = "jcushman"\n')
    (tmp_path / "repos" / "a" / ".git").mkdir(parents=True)

    result = _check_pending_repos(Context(workspace=ws))
    assert result.ok
    assert "every directory" in result.message


def test_non_repo_dirs_ignores_dotfiles(tmp_path):
    from fonds.git import non_repo_dirs

    (tmp_path / "wordscape").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "checkout" / ".git").mkdir(parents=True)

    assert non_repo_dirs(tmp_path) == {"wordscape"}
