"""Plugin discovery.

Three sources, in ascending order of specificity:

1. modules shipped in this package
2. the `fonds.plugins` entry point group, for installed third-party plugins
3. `<workspace>/plugins/*.py`, for one-off columns and checks a workspace wants

Discovery is by presence, not registration: a module is a plugin if it defines
a module-level `PLUGIN`.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from functools import cache
from importlib.metadata import entry_points
from pathlib import Path

import click

from ..api import Plugin

# Order matters only for CLI listing and column order in the inventory.
BUILTIN_ORDER = ["clone", "inventory", "tags", "deps", "secrets", "guards"]


def _module_plugin(module) -> Plugin | None:
    plugin = getattr(module, "PLUGIN", None)
    return plugin if isinstance(plugin, Plugin) else None


@cache
def builtin_plugins() -> tuple[Plugin, ...]:
    found: dict[str, Plugin] = {}
    for info in pkgutil.iter_modules(__path__):
        module = importlib.import_module(f"{__name__}.{info.name}")
        if plugin := _module_plugin(module):
            found[info.name] = plugin
    ordered = [found.pop(name) for name in BUILTIN_ORDER if name in found]
    ordered.extend(found[name] for name in sorted(found))
    return tuple(ordered)


@cache
def entry_point_plugins() -> tuple[Plugin, ...]:
    found = []
    for entry_point in entry_points(group="fonds.plugins"):
        loaded = entry_point.load()
        plugin = loaded() if callable(loaded) and not isinstance(loaded, Plugin) else loaded
        if not isinstance(plugin, Plugin):
            raise click.ClickException(
                f"Entry point 'fonds.plugins:{entry_point.name}' did not provide a Plugin."
            )
        found.append(plugin)
    return tuple(found)


def workspace_plugins(root: Path | None) -> tuple[Plugin, ...]:
    """Load `<workspace>/plugins/*.py`."""
    if root is None:
        return ()
    plugin_dir = root / "plugins"
    if not plugin_dir.is_dir():
        return ()

    found = []
    for path in sorted(plugin_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"fonds_workspace_plugins.{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        if plugin := _module_plugin(module):
            found.append(plugin)
    return tuple(found)


def all_plugins(root: Path | None = None) -> tuple[Plugin, ...]:
    return builtin_plugins() + entry_point_plugins() + workspace_plugins(root)
