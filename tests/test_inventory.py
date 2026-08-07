"""Row building, the slow-column cache, and plugin discovery."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fonds.api import Column, Context
from fonds.plugins import builtin_plugins
from fonds.plugins.inventory import COLUMNS, build_row
from fonds.repo import Repo
from fonds.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "fonds.toml").write_text('owner = "jcushman"\n')
    return Workspace.load(tmp_path)


@pytest.fixture
def repo():
    return Repo(
        name="alpha",
        owner="jcushman",
        description="a repo",
        url="https://github.com/jcushman/alpha",
        default_branch="main",
        private=True,
        archived=False,
        stars=3,
        updated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        languages={"Python": 900, "HTML": 100},
    )


def _columns(*names):
    return [c for c in COLUMNS if c.name in names]


def test_renders_core_columns(workspace, repo):
    row, cached = build_row(
        repo,
        Context(workspace=workspace),
        _columns("Name", "Visibility", "Stars", "Languages", "Last Updated"),
    )
    assert row["Name"] == "alpha"
    assert row["Visibility"] == "Private"
    assert row["Stars"] == 3
    assert row["Languages"] == "Python 90%, HTML 10%"
    assert row["Last Updated"] == "2026-08-01"
    assert cached is False


def test_slow_columns_are_reused_when_the_repo_has_not_changed(workspace, repo):
    calls = []

    def expensive(r, ctx):
        calls.append(r.name)
        return "fresh"

    columns = [*_columns("Name", "Last Updated"), Column("Costly", expensive, slow=True)]
    existing = {"Name": "alpha", "Last Updated": "2026-08-01", "Costly": "remembered"}

    row, cached = build_row(repo, Context(workspace=workspace, existing=existing), columns)
    assert cached is True
    assert row["Costly"] == "remembered"
    assert calls == []


def test_slow_columns_are_refetched_when_the_repo_changed(workspace, repo):
    columns = [*_columns("Name", "Last Updated"), Column("Costly", lambda r, c: "fresh", slow=True)]
    existing = {"Name": "alpha", "Last Updated": "2020-01-01", "Costly": "stale"}

    row, cached = build_row(repo, Context(workspace=workspace, existing=existing), columns)
    assert cached is False
    assert row["Costly"] == "fresh"


def test_missing_slow_column_defeats_the_cache(workspace, repo):
    """A column added since the last run must be filled in, not skipped."""
    columns = [*_columns("Name", "Last Updated"), Column("New", lambda r, c: "value", slow=True)]
    existing = {"Name": "alpha", "Last Updated": "2026-08-01"}

    row, cached = build_row(repo, Context(workspace=workspace, existing=existing), columns)
    assert cached is False
    assert row["New"] == "value"


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------

def test_builtin_plugins_are_discovered_by_presence():
    names = {plugin.name for plugin in builtin_plugins()}
    assert {"clone", "inventory", "tags", "deps", "secrets", "guards"} <= names


def test_every_builtin_plugin_contributes_something():
    for plugin in builtin_plugins():
        assert plugin.commands or plugin.columns or plugin.checks, plugin.name


def test_workspace_plugins_are_loaded_from_the_workspace(tmp_path):
    from fonds.plugins import workspace_plugins

    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "extra.py").write_text(
        "from fonds.api import Column, Plugin\n"
        "PLUGIN = Plugin(name='extra', columns=(Column('Owner', lambda r, c: r.owner),))\n"
    )
    plugins = workspace_plugins(tmp_path)
    assert [p.name for p in plugins] == ["extra"]
    assert plugins[0].columns[0].name == "Owner"


def test_workspace_plugin_column_reaches_the_inventory(tmp_path, repo):
    from fonds.plugins.inventory import all_columns

    (tmp_path / "fonds.toml").write_text('owner = "jcushman"\n')
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "extra.py").write_text(
        "from fonds.api import Column, Plugin\n"
        "PLUGIN = Plugin(name='extra', columns=(Column('Owner', lambda r, c: r.owner),))\n"
    )
    names = [column.name for column in all_columns(Workspace.load(tmp_path))]
    assert "Owner" in names
    assert names[0] == "Name"  # core columns still lead
