"""The sealed graph seed cache is a validated shortcut; the DB is authoritative."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import networkx as nx
from sqlalchemy import select

from repowise.core.ingestion.parse_cache import parser_fingerprint
from repowise.core.persistence import (
    batch_upsert_graph_edges,
    create_engine,
    create_session_factory,
    get_session,
    init_db,
)
from repowise.core.persistence.crud import upsert_repository
from repowise.core.persistence.database import resolve_db_url
from repowise.core.persistence.models import Repository
from repowise.core.pipeline.incremental.graph_seed import (
    invalidate_seed_edge_cache,
    load_seed_edges,
    save_seed_edge_cache,
)


def _gb() -> SimpleNamespace:
    graph = nx.DiGraph()
    graph.add_edge(
        "a.py",
        "b.py",
        edge_type="imports",
        imported_names=["b"],
        confidence=1.0,
    )
    graph.add_edge(
        "a.py::run",
        "b.py::work",
        edge_type="calls",
        call_lines=[4],
        resolution_origin="imported_name",
        confidence=0.9,
    )
    graph.add_edge("a.py::A", "a.py::A::method", edge_type="has_method")
    graph.add_edge(
        "a.py::A",
        "a.py::A::method",
        edge_type="has_method",
        call_lines=[9],
        resolution_origin="same_file",
        confidence=1.0,
    )
    graph.add_edge("a.py", "c.py", edge_type="type_use", imported_names=["C"])
    return SimpleNamespace(graph=lambda: graph)


def _seed_identity(rows: list[dict]) -> set[tuple]:
    return {
        (
            row["source_node_id"],
            row["target_node_id"],
            row["edge_type"],
            tuple(row.get("imported_names", [])),
            tuple(row.get("call_lines", [])),
        )
        for row in rows
    }


async def _repo_db(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".repowise").mkdir(parents=True)
    monkeypatch.setenv("REPOWISE_DB_URL", f"sqlite+aiosqlite:///{root / '.repowise' / 'wiki.db'}")
    engine = create_engine(resolve_db_url(root))
    await init_db(engine)
    sf = create_session_factory(engine)
    async with get_session(sf) as session:
        repo = await upsert_repository(
            session,
            name="repo",
            local_path=str(root.resolve()),
            head_commit="a" * 40,
        )
        repo.graph_edges_parser_fingerprint = parser_fingerprint()
        await batch_upsert_graph_edges(
            session,
            repo.id,
            [
                {
                    "source_node_id": "a.py",
                    "target_node_id": "b.py",
                    "edge_type": "imports",
                    "imported_names_json": '["b"]',
                },
                {
                    "source_node_id": "a.py::run",
                    "target_node_id": "b.py::work",
                    "edge_type": "calls",
                    "call_lines_json": "[4]",
                    "resolution_origin": "imported_name",
                    "confidence": 0.9,
                },
                {
                    "source_node_id": "a.py::A",
                    "target_node_id": "a.py::A::method",
                    "edge_type": "has_method",
                    "call_lines_json": "[9]",
                    "resolution_origin": "same_file",
                    "confidence": 1.0,
                },
                {
                    "source_node_id": "a.py",
                    "target_node_id": "c.py",
                    "edge_type": "type_use",
                    "imported_names_json": '["C"]',
                },
            ],
        )
    return root, engine, sf


async def test_seed_cache_round_trips_only_reusable_edges(tmp_path: Path, monkeypatch) -> None:
    root, engine, _sf = await _repo_db(tmp_path, monkeypatch)
    try:
        await save_seed_edge_cache(root, _gb())
        cache = root / ".repowise" / "graph_seed_edges.pkl"
        assert cache.is_file()
        rows = list(await load_seed_edges(root))
        assert _seed_identity(rows) == {
            ("a.py", "b.py", "imports", ("b",), ()),
            ("a.py::run", "b.py::work", "calls", (), (4,)),
            ("a.py::A", "a.py::A::method", "has_method", (), (9,)),
        }
    finally:
        await engine.dispose()


async def test_cache_stamp_mismatch_falls_back_to_database(tmp_path: Path, monkeypatch) -> None:
    root, engine, sf = await _repo_db(tmp_path, monkeypatch)
    try:
        await save_seed_edge_cache(root, _gb())
        async with get_session(sf) as session:
            repo_id = (await session.execute(select(Repository.id))).scalar_one()
            await batch_upsert_graph_edges(
                session,
                repo_id,
                [
                    {
                        "source_node_id": "d.py",
                        "target_node_id": "e.py",
                        "edge_type": "imports",
                        "imported_names_json": "[]",
                    }
                ],
            )

        rows = list(await load_seed_edges(root))
        assert ("d.py", "e.py", "imports", (), ()) in _seed_identity(rows)
    finally:
        await engine.dispose()


async def test_stale_edge_fingerprint_disables_patch_seed(tmp_path: Path, monkeypatch) -> None:
    root, engine, sf = await _repo_db(tmp_path, monkeypatch)
    try:
        await save_seed_edge_cache(root, _gb())
        async with get_session(sf) as session:
            repo = (await session.execute(select(Repository))).scalar_one()
            repo.graph_edges_parser_fingerprint = "old-resolver-fingerprint"
        assert await load_seed_edges(root) is None
    finally:
        await engine.dispose()


def test_invalidation_removes_snapshot_before_update_write(tmp_path: Path) -> None:
    cache = tmp_path / ".repowise" / "graph_seed_edges.pkl"
    cache.parent.mkdir()
    cache.write_bytes(b"previous snapshot")

    invalidate_seed_edge_cache(tmp_path)

    assert not cache.exists()
