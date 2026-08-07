"""The merge-preserving table is the part most worth pinning down: it is the
only place the tool writes over something a human may have edited."""

from __future__ import annotations

import pytest

from fonds.table import CsvFormat, MarkdownFormat, find_table, order_fields, read_table

COLUMNS = ["Name", "Description", "Tags"]


@pytest.fixture
def markdown(tmp_path):
    path = tmp_path / "all.md"
    path.write_text(
        "# My repos\n"
        "\n"
        "Some prose I wrote by hand.\n"
        "\n"
        "| Name | Description | Tags | Note |\n"
        "| --- | --- | --- | --- |\n"
        "| alpha | first repo | tools | keep |\n"
        "| beta | second repo |  | retire? |\n"
        "\n"
        "A footer paragraph.\n"
    )
    return path


def test_reads_rows_and_extra_columns(markdown):
    rows = MarkdownFormat().read(markdown)
    assert [row["Name"] for row in rows] == ["alpha", "beta"]
    assert rows[0]["Note"] == "keep"


def test_update_preserves_surrounding_prose_and_extra_columns(markdown):
    fmt = MarkdownFormat()
    stale = fmt.update(
        markdown,
        [{"Name": "alpha", "Description": "first repo, renamed", "Tags": "tools"}],
        COLUMNS,
        prune=False,
    )

    text = markdown.read_text()
    assert "# My repos" in text
    assert "Some prose I wrote by hand." in text
    assert "A footer paragraph." in text
    # The hand-added column and the row the tool didn't see both survive.
    assert "keep" in text
    assert "retire?" in text
    assert "first repo, renamed" in text
    assert stale == ["beta"]


def test_prune_drops_rows_the_source_no_longer_has(markdown):
    fmt = MarkdownFormat()
    fmt.update(markdown, [{"Name": "alpha"}], COLUMNS, prune=True)
    assert "beta" not in markdown.read_text()
    assert "alpha" in markdown.read_text()


def test_update_does_not_clobber_unlisted_fields(markdown):
    """A partial row updates its own keys and leaves the rest alone."""
    fmt = MarkdownFormat()
    fmt.update(markdown, [{"Name": "alpha", "Tags": "tools, active"}], COLUMNS, prune=False)
    rows = {row["Name"]: row for row in fmt.read(markdown)}
    assert rows["alpha"]["Description"] == "first repo"
    assert rows["alpha"]["Tags"] == "tools, active"


def test_pipes_in_descriptions_survive_a_round_trip(tmp_path):
    path = tmp_path / "all.md"
    fmt = MarkdownFormat()
    rows = [{"Name": "alpha", "Description": "does a | b | c", "Tags": ""}]
    fmt.write(rows, path, COLUMNS)
    assert fmt.read(path) == rows


def test_rewriting_unchanged_rows_is_a_no_op(tmp_path):
    """The nightly job commits the inventory, so an unchanged run must produce
    a byte-identical file or every run looks like a change."""
    path = tmp_path / "all.md"
    fmt = MarkdownFormat()
    rows = [
        {"Name": "alpha", "Description": "does things", "Tags": "tools"},
        {"Name": "beta", "Description": "", "Tags": ""},
    ]
    fmt.write(rows, path, COLUMNS)

    first = path.read_text()
    fmt.update(path, fmt.read(path), COLUMNS, prune=False)
    assert path.read_text() == first


def test_find_table_ignores_prose_that_is_not_a_table():
    assert find_table(["# Title", "", "not | a | table", ""]) is None


def test_order_fields_puts_known_columns_first():
    rows = [{"Zebra": 1, "Name": "a"}]
    assert order_fields(rows, COLUMNS) == ["Name", "Description", "Tags", "Zebra"]


def test_csv_round_trip(tmp_path):
    path = tmp_path / "all.csv"
    fmt = CsvFormat()
    rows = [{"Name": "alpha", "Description": "x", "Tags": "t"}]
    fmt.write(rows, path, COLUMNS)
    assert fmt.read(path) == rows


def test_read_table_rejects_unknown_extension(tmp_path):
    path = tmp_path / "all.txt"
    path.write_text("")
    with pytest.raises(Exception, match="Unsupported file extension"):
        read_table(path)
