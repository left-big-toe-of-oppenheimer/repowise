"""Unused exports whose declaration runs a call when the module loads.

``export const actionZoom = register({...})`` is never named again, yet
deleting the line removes what ``register`` did at import time (an entry in a
registry that is looked up by string). No reference search can see that, so
such a finding keeps its confidence but is never deletion-ready: the deletion
gate reads :attr:`DeadCodeFindingData.runs_at_load`.
"""

from __future__ import annotations

import re

from .models import DeadCodeFindingData, DeadCodeKind

#: Kinds whose declaration can carry an initializer.
_VALUE_KINDS = frozenset({"constant", "variable"})

#: The assignment of a declaration: one ``=`` that is not part of ``==``,
#: ``=>``, ``<=``, ``>=`` or ``!=``.
_ASSIGN_RE = re.compile(r"(?<![=!<>:])=(?![=>])")

#: An initializer that starts by calling something: ``f(``, ``a.b(``,
#: ``new X(``, ``await f(``, ``f<T>(``. Function and arrow literals do not
#: run their body, so ``function``, ``async`` and ``func`` heads are not calls.
_CALL_RE = re.compile(
    r"(?:await\s+)?(?:new\s+)?"
    r"(?!(?:function|async|func|fun|lambda)\b)"
    r"[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*\s*(?:<[^()=;]*>)?\s*\("
)

#: How much of the declaration to read: the initializer starts on its first lines.
_HEAD_CHARS = 600


def mark_load_time_calls(
    findings: list[DeadCodeFindingData], source_map: dict[str, bytes]
) -> list[DeadCodeFindingData]:
    """Set ``runs_at_load`` on unused value exports initialized by a call.

    Mutates in place and returns the same list.
    """
    for finding in findings:
        if _candidate(finding) and _initialized_by_call(finding, source_map):
            finding.runs_at_load = True
    return findings


def _candidate(finding: DeadCodeFindingData) -> bool:
    return (
        finding.kind is DeadCodeKind.UNUSED_EXPORT
        and finding.symbol_kind in _VALUE_KINDS
        and finding.is_spanned_symbol
    )


def _initialized_by_call(finding: DeadCodeFindingData, source_map: dict[str, bytes]) -> bool:
    blob = source_map.get(finding.file_path)
    if blob is None or finding.start_line is None or finding.end_line is None:
        return False
    lines = blob.decode("utf-8", errors="replace").splitlines()
    head = "\n".join(lines[finding.start_line - 1 : finding.end_line])[:_HEAD_CHARS]
    name = re.search(rf"(?<![\w$]){re.escape(finding.symbol_name or '')}(?![\w$])", head)
    if name is None:
        return False
    assign = _ASSIGN_RE.search(head, name.end())
    return assign is not None and _CALL_RE.match(head[assign.end() :].lstrip()) is not None


__all__ = ["mark_load_time_calls"]
