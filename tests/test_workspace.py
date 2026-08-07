from __future__ import annotations

import pytest

from fonds.repo import Repo, assign_dir_names, matches_patterns
from fonds.workspace import Workspace


def write_workspace(tmp_path, config: str):
    (tmp_path / "fonds.toml").write_text(config)
    return tmp_path


def test_owner_shorthand_becomes_a_single_source(tmp_path):
    root = write_workspace(tmp_path, 'owner = "jcushman"\n')
    workspace = Workspace.load(root)
    assert [s.owner for s in workspace.config.sources] == ["jcushman"]


def test_sources_long_form_with_filters(tmp_path):
    root = write_workspace(
        tmp_path,
        """
        [[sources]]
        owner = "harvard-lil"
        include = ["perma*"]
        exclude = ["*-old"]
        """,
    )
    source = Workspace.load(root).config.sources[0]
    assert source.include == ("perma*",)
    assert source.exclude == ("*-old",)


def test_owner_and_sources_together_is_an_error(tmp_path):
    root = write_workspace(tmp_path, 'owner = "a"\n[[sources]]\nowner = "b"\n')
    with pytest.raises(Exception, match="not both"):
        Workspace.load(root)


def test_no_source_is_an_error(tmp_path):
    root = write_workspace(tmp_path, "# nothing here\n")
    with pytest.raises(Exception, match="no repo sources"):
        Workspace.load(root)


def test_find_walks_up_from_a_subdirectory(tmp_path):
    root = write_workspace(tmp_path, 'owner = "jcushman"\n')
    nested = root / "repos" / "some-repo" / "src"
    nested.mkdir(parents=True)
    assert Workspace.find_root(nested) == root


def test_find_returns_none_outside_a_workspace(tmp_path):
    assert Workspace.find_root(tmp_path) is None


def test_env_var_overrides_discovery(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    write_workspace(ws, 'owner = "jcushman"\n')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.setenv("FONDS_WORKSPACE", str(ws))
    assert Workspace.find_root(elsewhere) == ws


def test_plugin_settings_read_their_own_table(tmp_path):
    root = write_workspace(
        tmp_path,
        'owner = "jcushman"\n\n[guards]\nprivate_pages_allowlist = ["showcase"]\n',
    )
    workspace = Workspace.load(root)
    assert workspace.plugin_settings("guards") == {
        "private_pages_allowlist": ["showcase"]
    }
    assert workspace.plugin_settings("nope") == {}


def test_conventional_paths(tmp_path):
    workspace = Workspace.load(write_workspace(tmp_path, 'owner = "x"\n'))
    assert workspace.repos_dir == tmp_path / "repos"
    assert workspace.inventory_path == tmp_path / "inventory" / "all.md"
    assert workspace.tag_dir("ops") == tmp_path / "tags" / "ops"
    assert workspace.data_dir("deps") == tmp_path / "data" / "deps"


# ---------------------------------------------------------------------------
# Directory naming
# ---------------------------------------------------------------------------

def test_single_owner_stays_flat():
    """The common case must not pay for the multi-source case."""
    repos = assign_dir_names([Repo("alpha", "jcushman"), Repo("beta", "jcushman")])
    assert [r.dir_name for r in repos] == ["alpha", "beta"]


def test_only_colliding_names_get_an_owner_prefix():
    repos = assign_dir_names(
        [
            Repo("shared", "jcushman"),
            Repo("shared", "harvard-lil"),
            Repo("solo", "harvard-lil"),
        ]
    )
    by_key = {(r.owner, r.name): r.dir_name for r in repos}
    assert by_key[("jcushman", "shared")] == "jcushman__shared"
    assert by_key[("harvard-lil", "shared")] == "harvard-lil__shared"
    assert by_key[("harvard-lil", "solo")] == "solo"


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,include,exclude,expected",
    [
        ("perma", (), (), True),
        ("perma", ("perma*",), (), True),
        ("scoop", ("perma*",), (), False),
        ("perma-old", ("perma*",), ("*-old",), False),
        ("perma", (), ("perma",), False),
    ],
)
def test_matches_patterns(name, include, exclude, expected):
    assert matches_patterns(name, include, exclude) is expected
