"""Whole-repo source scans run once when the analyzer is built."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .dynamic_markers import read_source_text


def _find_jsx_namespace_files(
    parsed_files: dict,
    source_map: dict[str, bytes] | None = None,
) -> set[str]:
    """Repo-relative TS/TSX files that declare ``namespace JSX``.

    Types there are used implicitly by every JSX expression, never imported
    by name. A substring check is enough.
    """
    matches: set[str] = set()
    for path, pf in parsed_files.items():
        try:
            file_info = getattr(pf, "file_info", None)
            if file_info is None:
                continue
            src_path = Path(file_info.abs_path)
            if src_path.suffix not in (".ts", ".tsx", ".d.ts"):
                continue
            source = read_source_text(path, file_info.abs_path, source_map)
            if "namespace JSX" in source:
                matches.add(path)
        except Exception:
            continue
    return matches


_BUNDLER_CONFIG_STEMS = ("vite.config", "webpack.config", "rollup.config", "rspack.config")

# Any quoted slash-containing path-ish string. Bundler configs reference
# alias targets both as ``'./src/shims/x.ts'`` and as bare segments fed to
# ``path.resolve(here, 'src/shims/x.ts')``; the parsed-files membership
# probe below keeps loose matches harmless.
_RELATIVE_PATH_STRING_RE = re.compile(r"""['"]((?:\.\.?/)?[\w@.-]+(?:/[\w@.-]+)+)['"]""")

_TS_PROBE_SUFFIXES = ("", ".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")


def _find_bundler_alias_targets(
    parsed_files: dict,
    source_map: dict[str, bytes] | None = None,
) -> set[str]:
    """Repo-relative paths referenced from bundler config files.

    A ``resolve.alias`` entry substitutes a package import with a local shim
    that only the config names. Any relative path string in a bundler config
    that resolves to an indexed file marks that file reachable.
    """
    targets: set[str] = set()
    configs = [
        (path, pf)
        for path, pf in parsed_files.items()
        if Path(path).name.startswith(_BUNDLER_CONFIG_STEMS)
    ]
    for path, pf in configs:
        try:
            file_info = getattr(pf, "file_info", None)
            if file_info is None:
                continue
            source = read_source_text(path, file_info.abs_path, source_map)
        except Exception:
            continue
        config_dir = Path(path).parent
        for match in _RELATIVE_PATH_STRING_RE.finditer(source):
            base = _posix_normpath(config_dir, match.group(1))
            for suffix in _TS_PROBE_SUFFIXES:
                candidate = base + suffix
                if candidate in parsed_files:
                    targets.add(candidate)
                    break
    return targets


def _posix_normpath(config_dir: Path, raw: str) -> str:
    """Join *raw* onto *config_dir* and normalise ``.``/``..`` as POSIX."""
    import posixpath

    return posixpath.normpath(posixpath.join(config_dir.as_posix(), raw))


def _find_ts_export_aliases(parsed_files: dict) -> dict[str, dict[str, str]]:
    """Per-file ``{local_name: exported_alias}`` maps for TS/JS alias exports.

    ``export { Local as Alias }`` publishes the symbol under the alias, so
    importers name only ``Alias``. Inverted from the parser's record of the
    clause rather than scanned again.
    """
    aliases: dict[str, dict[str, str]] = {}
    for path, pf in parsed_files.items():
        published = getattr(pf, "export_aliases", None)
        if not published:
            continue
        aliases[path] = {local: exported for exported, local in published.items()}
    return aliases


#: ``export default function name`` / ``export default class Name``.
_TS_DEFAULT_DECL_RE = re.compile(
    r"^\s*export\s+default\s+(?:async\s+)?(?:function\s*\*?|(?:abstract\s+)?class)\s+"
    r"([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
#: ``export default name;`` naming a declaration elsewhere in the file.
_TS_DEFAULT_REF_RE = re.compile(r"^\s*export\s+default\s+([A-Za-z_$][\w$]*)\s*;?\s*$", re.MULTILINE)
_TS_JS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _find_ts_default_exports(
    parsed_files: dict, source_map: dict[str, bytes] | None = None
) -> dict[str, str]:
    """``{file: the local name its default export publishes}`` for TS/JS files.

    An importer binds a default export under any local name it likes
    (``import mathRule from "./math"``), so only the binding's ``default``
    links it to ``markdownMath``.
    """
    defaults: dict[str, str] = {}
    for path, pf in parsed_files.items():
        file_info = getattr(pf, "file_info", None)
        if file_info is None or not path.endswith(_TS_JS_SUFFIXES):
            continue
        source = read_source_text(path, file_info.abs_path, source_map)
        if "export default" not in source:
            continue
        match = _TS_DEFAULT_DECL_RE.search(source) or _TS_DEFAULT_REF_RE.search(source)
        if match:
            defaults[path] = match.group(1)
    return defaults


def _find_bound_export_names(parsed_files: dict) -> dict[str, set[str]]:
    """``{file: the exported names its importers bind}``, from import bindings.

    An edge's ``imported_names`` are the importer's local names; the binding
    keeps the name the target exports, which is what a default import needs.
    """
    bound: dict[str, set[str]] = {}
    for binding in _import_bindings(parsed_files):
        if binding.source_file and binding.exported_name:
            bound.setdefault(binding.source_file, set()).add(binding.exported_name)
    return bound


def _import_bindings(parsed_files: dict) -> Iterator[Any]:
    for pf in parsed_files.values():
        for imp in getattr(pf, "imports", None) or ():
            yield from getattr(imp, "bindings", None) or ()
