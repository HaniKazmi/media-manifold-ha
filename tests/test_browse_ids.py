"""Every content id the tree can mint must have a browse handler.

A node whose id no handler recognises looks completely normal until someone
opens it, then dead-ends with "cannot browse ...". Nothing in the type system
connects the two sites, so this walks the source instead: the kinds passed to
`_id()` and `_directory()` are compared against the kinds `_async_browse`
dispatches on. The kinds an item can carry are data rather than call sites, so
those come from `ITEM_KINDS` and `_ARTIST_VIEWS` directly.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from custom_components.sonos_apple_music.applemusic.browse import (
    _ARTIST_VIEWS,
    ITEM_KINDS,
)

SOURCE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "sonos_apple_music"
    / "applemusic"
    / "browse.py"
)
TREE = ast.parse(SOURCE.read_text())

# Leaves: minted with can_expand=False, so they are never browsed into. A
# station has no track list at all — Sonos streams it and picks what comes next.
LEAF_KINDS = {"song", "station"}


def _minted_kinds() -> set[str]:
    """Every kind the tree can put into a content id.

    Item and artist-view kinds come from the tables that build them — both are
    minted from a loop variable, which no literal argument names — and the rest
    are literal first/second args to _id() or _directory().
    """
    kinds: set[str] = {kind for kind, *_ in ITEM_KINDS.values()} | set(_ARTIST_VIEWS)
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "_id" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                kinds.add(arg.value)
        elif node.func.id == "_directory" and len(node.args) >= 2:
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                kinds.add(arg.value)
    return kinds


def _handled_kinds() -> set[str]:
    """Kinds _async_browse dispatches on, via == , in, or startswith."""
    handler = next(
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_async_browse"
    )
    kinds: set[str] = set()
    prefixes: set[str] = set()
    for node in ast.walk(handler):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
            if node.left.id != "kind":
                continue
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant):
                    kinds.add(comparator.value)
                elif isinstance(comparator, (ast.Tuple, ast.List)):
                    kinds.update(
                        e.value for e in comparator.elts if isinstance(e, ast.Constant)
                    )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "kind"
        ):
            prefixes.update(
                a.value for a in node.args if isinstance(a, ast.Constant)
            )

    # Expand prefix handlers against the kinds actually minted.
    for kind in _minted_kinds():
        if any(kind.startswith(p) for p in prefixes):
            kinds.add(kind)
    return kinds


def test_the_tree_mints_something() -> None:
    """Guard against the walk silently matching nothing and passing vacuously."""
    assert len(_minted_kinds()) >= 6


@pytest.mark.parametrize("kind", sorted(_minted_kinds() - LEAF_KINDS))
def test_every_minted_kind_is_browsable(kind: str) -> None:
    assert kind in _handled_kinds(), (
        f"browse.py mints 'apple-music://{kind}/…' but _async_browse does not "
        f"handle it, so opening that node dead-ends"
    )
