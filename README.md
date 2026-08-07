# fonds

Manage a **metarepo**: one directory holding every repo you care about, plus an
inventory of what they are.

> A *fonds*, in archival practice, is the whole body of records produced by one
> source, kept together because their provenance is what makes them meaningful.
> This is that, for repos.

```console
$ fonds init jcushman        # create a workspace
$ fonds inventory            # build inventory/all.md
$ fonds clone                # clone every repo into repos/
$ fonds check                # assert the things that should stay true
```

## Why

Working across many repos is mostly a discovery problem. Which repo has the
deploy config? What still uses that library? What have we not touched in three
years, and is any of it published to the web by accident?

Those questions are easy when everything is on disk at once and there is a
single table describing it — and they are what coding agents need too, since an
agent can grep `repos/` and read `inventory/all.md` instead of guessing.

`fonds` maintains both, for a GitHub user or an organization.

## Install

```console
$ uv tool install git+https://github.com/jcushman/fonds
```

Or run it without installing:

```console
$ uvx --from git+https://github.com/jcushman/fonds fonds --help
```

Authentication is whatever you already have: `GITHUB_TOKEN`, a `.github-token`
file in the workspace, or an existing `gh auth login` session.

## The workspace

A workspace is any directory containing `fonds.toml`. Commands find it by
walking up from the current directory, the way `git` finds `.git`, so the tool
is installed once and operates on whichever workspace you are standing in.

Layout is convention, not configuration:

```
fonds.toml          the marker file, and the little config there is
repos/              a checkout of every repo
wikis/              a checkout of every GitHub wiki that has content
inventory/all.md    the table; plus one file per tag with --all-tags
tags/<tag>/         optional focused checkouts of a tagged subset
data/<plugin>/      artifacts plugins cache between runs
local/              git-ignored scratch space
plugins/            optional workspace-local plugins
```

The whole of a typical `fonds.toml`:

```toml
owner = "jcushman"
```

Multiple sources, with filters, when you need them:

```toml
[[sources]]
owner = "jcushman"
exclude = ["*-fork"]

[[sources]]
owner = "harvard-lil"
include = ["perma*", "scoop*"]
```

Checkouts stay flat — `repos/perma`, not `repos/harvard-lil/perma`. Only names
claimed by two owners at once get disambiguated, so a second source never
imposes a directory level on repos that don't need one.

## The inventory

`inventory/all.md` is a Markdown table of every repo: description, URL, wiki,
Pages status, default and other branches, visibility, fork/archive state, stars,
last update, contributors, languages, and tags.

It is generated, but **safe to edit**. Regenerating merges: columns you add,
prose above and below the table, and rows for repos the tool cannot see are all
preserved. Pass `--prune` to actually drop rows for repos that no longer exist.

That matters because it makes the table an editing surface. Add tags in the
Tags column, then push them back:

```console
$ fonds tags sync
```

Columns that cost a network round trip per repo — contributors, wiki existence —
are reused from the previous run unless the repo has changed since, so a rebuild
over hundreds of repos is mostly cached.

`--format csv` and `--format excel` also work.

## Tags

Tags are the workspace's own vocabulary, and `--tag` filters on them everywhere:

```console
$ fonds clone --tag ops        # into tags/ops/
$ fonds secrets --tag ops
$ fonds inventory --all-tags   # inventory/ops.md, inventory/perma.md, ...
```

Where they are stored depends on the account, and is chosen for you:
organizations get the `tags` custom property (which has a schema of allowed
values); user accounts have no custom properties, so those use repo topics.
Nothing to configure.

## Commands

| | |
| --- | --- |
| `fonds init OWNER` | Create a workspace, with a nightly inventory workflow |
| `fonds clone` | Clone every repo, or update existing checkouts |
| `fonds hydrate` | Widen shallow, single-branch checkouts into full ones |
| `fonds inventory` | Rebuild the inventory table |
| `fonds tags` | `list`, `add`, `remove`, `sync` |
| `fonds deps` | `scan`, `intra`, `top`, `external` |
| `fonds secrets` | Scan checkouts for leaked credentials |
| `fonds check` | Run every registered assertion |
| `fonds doctor` | Show config, auth, plugins, and missing tools |

Clones are shallow and single-branch, which is what makes a few-hundred-repo
workspace practical to keep current; `hydrate` widens the ones you need. Nothing
ever touches a checkout with uncommitted or unpushed work.

`fonds deps` needs [cdxgen](https://github.com/CycloneDX/cdxgen) and
`fonds secrets` needs [trufflehog](https://github.com/trufflesecurity/trufflehog);
`fonds doctor` will tell you what is missing.

## Checks

`fonds check` runs assertions and exits non-zero on failure, which is what makes
a scheduled job worth having — an inventory nobody reads drifts, but a check
that fails does not.

Built in: no private repo serves GitHub Pages without being allowlisted (those
sites are world-readable, so this fails closed), and every active repo has a
default branch.

```toml
[guards]
private_pages_allowlist = ["binoc-showcase", "*-docs"]
```

`fonds init` writes a GitHub Actions workflow that rebuilds the inventory nightly
and commits it — skipping runs where only "last updated" dates moved.

## Plugins

Every feature above is a plugin, including `clone` and `inventory`; there is no
privileged path. A plugin is a module defining a module-level `PLUGIN`, and it
contributes through four hooks:

```python
from fonds.api import Column, Plugin

PLUGIN = Plugin(
    name="loc",
    columns=(Column("Lines", lambda repo, ctx: count_lines(repo), slow=True),),
)
```

| hook | adds |
| --- | --- |
| `commands` | click commands on the `fonds` CLI |
| `columns` | columns in the inventory table |
| `checks` | assertions run by `fonds check` |
| `requires` | external binaries reported by `fonds doctor` |

Plugins are found by presence in three places: shipped in `fonds/plugins/`,
installed under the `fonds.plugins` entry point group, or dropped into a
workspace's own `plugins/*.py` — the last being the cheap way to add one column
that only matters to one workspace.

## Prior art

[ghorg](https://github.com/gabrie30/ghorg) clones an org and does it well; if
that is all you need, use it. `fonds` is for the case where the checkout is
means rather than end, and what you actually want is the inventory, the tags,
and the questions you can ask once everything is in one place.

## License

MIT
