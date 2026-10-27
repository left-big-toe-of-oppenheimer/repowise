"""Decide whether an update can patch the graph instead of rebuilding it.

Resolving one file reads only that file plus, for every *other* file, what it
exposes: its symbols, its heritage and, for languages that re-export through
imports, its imports. If an edit changes none of that, no other file can
resolve differently, and re-resolving just the edited file reproduces a full
rebuild. This module classifies an edit by comparing the file's parse before
and after, taken from the parse cache.

The rule is deliberately conservative: anything it cannot prove falls back to
the full rebuild.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

# Languages whose imports are not part of a file's surface: another file never
# resolves a name *through* this file's imports. Java has no re-exports.
# Python (``__init__`` re-exports) and TS/JS (barrel files) are left out.
PATCHABLE_LANGUAGES = frozenset({"java"})
IMPORTS_IRRELEVANT = frozenset({"java"})


@dataclass(frozen=True)
class PatchPlan:
    """Whether to patch, which files to re-resolve, and why not when refusing."""

    eligible: bool
    scope: tuple[str, ...] = ()
    reason: str | None = None


def _symbol_key(sym: Any) -> tuple:
    return (
        sym.id,
        sym.name,
        sym.qualified_name,
        str(sym.kind),
        sym.signature,
        sym.visibility,
        tuple(sym.decorators),
        tuple(sym.modifiers),
        sym.parent_name,
        sym.is_declaration,
        sym.is_exported_symbol,
        sym.is_async,
        sym.type_parameter_count,
        sym.extension_receiver,
        sym.written_receiver,
        sym.language,
    )


def surface_fingerprint(parsed: Any) -> str:
    """Hash of everything other files can observe about *parsed*.

    Line numbers, docstrings, complexity and call sites are excluded: a body
    edit moves them without changing how any other file resolves.
    """
    h = hashlib.sha256()
    parts: list[Any] = [
        sorted(_symbol_key(s) for s in parsed.symbols),
        sorted((r.child_name, r.parent_name, str(r.kind)) for r in parsed.heritage),
        sorted(parsed.exports),
        sorted(parsed.export_aliases.items()),
    ]
    if parsed.file_info.language not in IMPORTS_IRRELEVANT:
        parts.append(
            sorted(
                (
                    i.module_path,
                    tuple(i.imported_names),
                    i.is_relative,
                    i.is_reexport,
                    i.type_only,
                    i.deferred,
                )
                for i in parsed.imports
            )
        )
    h.update(repr(parts).encode())
    return h.hexdigest()


def plan_patch(
    *,
    parse_cache: Any,
    current_paths: Collection[str],
    changed: dict[str, Any],
) -> PatchPlan:
    """Plan a patch for the files whose content changed since the last index.

    *changed* maps each changed path to its fresh :class:`ParsedFile`;
    *current_paths* is every path parsed in this run. The parse cache stands in
    for the previous run's manifest.
    """
    if parse_cache is None:
        return PatchPlan(False, reason="no parse cache")
    previous_paths = parse_cache.known_paths()
    if set(current_paths) != previous_paths:
        return PatchPlan(False, reason="files were added or removed")
    for path, parsed in sorted(changed.items()):
        language = parsed.file_info.language
        if language not in PATCHABLE_LANGUAGES:
            return PatchPlan(False, reason=f"{path}: {language} edits are not patched")
        before = parse_cache.previous_entry(path, exclude_hash=parsed.content_hash)
        if before is None:
            return PatchPlan(False, reason=f"{path}: new file or no earlier parse to compare")
        if surface_fingerprint(before) != surface_fingerprint(parsed):
            return PatchPlan(False, reason=f"{path}: its symbols or heritage changed")
    return PatchPlan(True, scope=tuple(sorted(changed)))
