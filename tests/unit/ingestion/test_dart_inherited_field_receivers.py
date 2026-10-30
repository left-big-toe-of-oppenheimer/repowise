"""A Dart receiver that names a field or getter of a superclass is typed from it."""

from __future__ import annotations

from pathlib import Path

from repowise.core.ingestion.call_resolver import CallResolver
from tests.unit.ingestion.test_call_resolver_strategies import _parse_all

_CACHE = "class Cache {\n  void clean() {}\n}\n\nclass Globals {\n  void find() {}\n}\n"
_BASE = """\
import 'cache.dart';

abstract class Command {
  late final Cache cache = Cache();

  Globals get globals => Globals();
}
"""


def _callees(tmp_path: Path, sub: str, parents: dict[str, set[str]]) -> list[str]:
    files = {"lib/cache.dart": _CACHE, "lib/command.dart": _BASE, "lib/clean.dart": sub}
    parsed = _parse_all(tmp_path, {k: ("dart", v) for k, v in files.items()})
    imports = {
        "lib/clean.dart": {"lib/command.dart", "lib/cache.dart"},
        "lib/command.dart": {"lib/cache.dart"},
        "lib/cache.dart": set(),
    }
    for path, targets in imports.items():
        for imp in parsed[path].imports:
            imp.resolved_file = next(t for t in targets if t.endswith(imp.module_path.rsplit("/", 1)[-1]))
    resolver = CallResolver(parsed, imports, repo_path=str(tmp_path), heritage_parents=parents)
    calls = parsed["lib/clean.dart"].calls
    return sorted(rc.callee_id for rc in resolver.resolve_file("lib/clean.dart", calls))


_PARENTS = {"lib/clean.dart::CleanCommand": {"lib/command.dart::Command"}}


def test_an_inherited_field_and_getter_type_their_receivers(tmp_path: Path) -> None:
    sub = """\
import 'cache.dart';
import 'command.dart';

class CleanCommand extends Command {
  void run() {
    cache.clean();
    globals.find();
  }
}
"""
    assert _callees(tmp_path, sub, _PARENTS) == [
        "lib/cache.dart::Cache::clean",
        "lib/cache.dart::Globals::find",
    ]


def test_a_local_of_the_same_name_hides_the_inherited_field(tmp_path: Path) -> None:
    sub = """\
import 'cache.dart';
import 'command.dart';

class CleanCommand extends Command {
  void run() {
    var cache = 1;
    cache.clean();
  }
}
"""
    assert _callees(tmp_path, sub, _PARENTS) == []
