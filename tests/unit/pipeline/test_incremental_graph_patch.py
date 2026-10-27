"""A patched graph must equal a full rebuild of the same tree.

``build_repo_graph(seed_edges=...)`` reuses the persisted import, call and
heritage edges and re-resolves only the files that changed, but only when the
edit provably left every changed file's cross-file surface alone. These tests
build a Java tree, persist its edges the way the index does, edit it, and
compare the patched graph with a from-scratch build of the edited tree: nodes
with their attributes, and every edge with the fields the index stores. Edits
that change a file's surface, add a file or touch a language the patcher does
not cover must fall back to the full build.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from repowise.core.ingestion import GraphBuilder
from repowise.core.pipeline.incremental import build_repo_graph

_FILES = {
    "src/com/acme/util/Util.java": """package com.acme.util;

public class Util {
    public static String id() { return "id"; }
    public static int twice(int x) { return x * 2; }
}
""",
    "src/com/acme/util/Clock.java": """package com.acme.util;

public class Clock {
    public static long now() { return 1L; }
}
""",
    "src/com/acme/svc/Base.java": """package com.acme.svc;

public class Base {
    public String name() { return "base"; }
}
""",
    "src/com/acme/svc/Service.java": """package com.acme.svc;

import com.acme.util.Util;

public class Service extends Base {
    public String run() {
        return Util.id() + name();
    }
}
""",
    "src/com/acme/svc/Other.java": """package com.acme.svc;

import com.acme.util.Util;

public class Other {
    private final Service service = new Service();
    public String go() {
        return service.run() + Util.twice(2);
    }
}
""",
}
_SERVICE = "src/com/acme/svc/Service.java"


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _seed_rows(graph_builder: Any) -> list[dict]:
    """The edges as the index persists them (``graph_edges`` columns, decoded)."""
    return [
        {
            "source_node_id": u,
            "target_node_id": v,
            "edge_type": d.get("edge_type", "imports"),
            "confidence": d.get("confidence", 1.0),
            "imported_names": d.get("imported_names", []),
            "hint_source": d.get("hint_source"),
            "resolution_origin": d.get("resolution_origin"),
            "call_lines": d.get("call_lines", []),
            "type_only": bool(d.get("type_only", False)),
            "deferred": bool(d.get("deferred", False)),
        }
        for u, v, d in graph_builder.graph().edges(data=True)
    ]


def _edges(graph_builder: Any) -> set[tuple]:
    return {
        (
            u,
            v,
            d.get("edge_type", "imports"),
            d.get("confidence", 1.0),
            d.get("hint_source"),
            d.get("resolution_origin"),
            tuple(sorted(d.get("imported_names", []))),
            tuple(d.get("call_lines", [])),
            bool(d.get("type_only", False)),
            bool(d.get("deferred", False)),
        )
        for u, v, d in graph_builder.graph().edges(data=True)
    }


def _nodes(graph_builder: Any) -> dict[str, dict]:
    return {n: dict(d) for n, d in graph_builder.graph().nodes(data=True)}


def _full(tree: Path, dest: Path) -> Any:
    """A from-scratch build of *tree*'s current contents, with its own empty cache."""
    shutil.copytree(tree, dest, ignore=shutil.ignore_patterns(".repowise"))
    return build_repo_graph(dest, [], collect_sources=True)[2]


@pytest.fixture
def indexed(tmp_path: Path) -> tuple[Path, list[dict]]:
    """A Java tree that has been built once, plus its persisted edges."""
    tree = tmp_path / "repo"
    _write(tree, _FILES)
    first = build_repo_graph(tree, [], collect_sources=True)[2]
    return tree, _seed_rows(first)


def _patched(tree: Path, seed: list[dict]) -> Any:
    graph_builder = build_repo_graph(
        tree, [], collect_sources=True, seed_edges=iter(seed)
    )[2]
    return graph_builder


def _assert_equals_full(tree: Path, patched: Any, tmp_path: Path) -> None:
    full = _full(tree, tmp_path / "full")
    assert _nodes(patched) == _nodes(full)
    assert _edges(patched) == _edges(full)


def test_body_edit_is_patched_and_equals_a_full_rebuild(
    indexed: tuple[Path, list[dict]], tmp_path: Path
) -> None:
    tree, seed = indexed
    (tree / _SERVICE).write_text(
        _FILES[_SERVICE]
        .replace(
            "import com.acme.util.Util;",
            "import com.acme.util.Clock;\nimport com.acme.util.Util;",
        )
        .replace("return Util.id() + name();", "return Util.id() + name() + Clock.now();")
    )

    patched = _patched(tree, seed)

    assert patched.patch_plan.eligible, patched.patch_plan.reason
    assert patched.patch_plan.scope == (_SERVICE,)
    _assert_equals_full(tree, patched, tmp_path)
    clock = "src/com/acme/util/Clock.java"
    assert patched.graph().has_edge(_SERVICE, clock)


def test_removing_an_import_and_call_is_patched(
    indexed: tuple[Path, list[dict]], tmp_path: Path
) -> None:
    tree, seed = indexed
    (tree / _SERVICE).write_text(
        _FILES[_SERVICE]
        .replace("import com.acme.util.Util;\n", "")
        .replace("return Util.id() + name();", 'return "x" + name();')
    )

    patched = _patched(tree, seed)

    assert patched.patch_plan.eligible, patched.patch_plan.reason
    _assert_equals_full(tree, patched, tmp_path)


def test_unchanged_tree_patches_to_the_same_graph(
    indexed: tuple[Path, list[dict]], tmp_path: Path
) -> None:
    tree, seed = indexed

    patched = _patched(tree, seed)

    assert patched.patch_plan.eligible
    assert patched.patch_plan.scope == ()
    _assert_equals_full(tree, patched, tmp_path)


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        pytest.param(
            lambda t: (t / _SERVICE).write_text(
                _FILES[_SERVICE].replace("public String run()", "public int run(int a)")
                .replace("return Util.id() + name();", "return a;")
            ),
            "symbols or heritage",
            id="signature-change",
        ),
        pytest.param(
            lambda t: (t / _SERVICE).write_text(
                _FILES[_SERVICE].replace(" extends Base", "")
            ),
            "symbols or heritage",
            id="heritage-change",
        ),
        pytest.param(
            lambda t: _write(
                t,
                {
                    "src/com/acme/svc/Extra.java": (
                        "package com.acme.svc;\npublic class Extra {}\n"
                    )
                },
            ),
            "new file",
            id="added-file",
        ),
        pytest.param(
            lambda t: (t / "src/com/acme/util/Clock.java").unlink(),
            "added or removed",
            id="removed-file",
        ),
    ],
)
def test_surface_changes_fall_back_to_a_full_rebuild(
    indexed: tuple[Path, list[dict]], tmp_path: Path, edit: Any, reason: str
) -> None:
    tree, seed = indexed
    edit(tree)

    patched = _patched(tree, seed)

    assert not patched.patch_plan.eligible
    assert reason in (patched.patch_plan.reason or "")
    _assert_equals_full(tree, patched, tmp_path)


def test_unsupported_language_falls_back(tmp_path: Path) -> None:
    tree = tmp_path / "repo"
    _write(tree, {**_FILES, "tools/helper.py": "def f():\n    return 1\n"})
    first = build_repo_graph(tree, [], collect_sources=True)[2]
    seed = _seed_rows(first)
    (tree / "tools/helper.py").write_text("def f():\n    return 2\n")

    patched = _patched(tree, seed)

    assert not patched.patch_plan.eligible
    assert "python edits are not patched" in (patched.patch_plan.reason or "")
    _assert_equals_full(tree, patched, tmp_path)


def test_no_seed_means_a_plain_full_build(indexed: tuple[Path, list[dict]]) -> None:
    tree, _seed = indexed

    graph_builder = build_repo_graph(tree, [], collect_sources=True)[2]

    assert not graph_builder.patch_plan.eligible
    assert graph_builder.patch_plan.reason == "not requested"


def test_patch_resolves_only_the_changed_file(
    indexed: tuple[Path, list[dict]], monkeypatch: pytest.MonkeyPatch
) -> None:
    tree, seed = indexed
    (tree / _SERVICE).write_text(_FILES[_SERVICE].replace("Util.id()", "Util.twice(1)"))
    resolved: list[str] = []
    original = GraphBuilder._add_import_edge

    def spy(self: Any, path: str, *args: Any) -> None:
        resolved.append(path)
        original(self, path, *args)

    monkeypatch.setattr(GraphBuilder, "_add_import_edge", spy)

    patched = _patched(tree, seed)

    assert patched.patch_plan.eligible
    assert set(resolved) == {_SERVICE}
