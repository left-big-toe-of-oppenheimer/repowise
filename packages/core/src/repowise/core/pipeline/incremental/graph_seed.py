"""Read and cache the persisted edges a graph patch reuses.

A Portal-sized SQLite table holds hundreds of thousands of import/call edges.
Loading those ORM rows for every patch is avoidable: after a successful index,
store the small, stable subset in a sealed local cache for sequential reads.
The cache is only a hint. Its repository ID, total edge count and parser
fingerprint must still match the authoritative database; otherwise the update
reads the rows from SQL and rebuilds the cache after persistence.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import structlog

from repowise.core.cache_seal import dump_sealed_pickle, load_sealed_pickle

log = structlog.get_logger(__name__)
_CACHE_NAME = "graph_seed_edges.pkl"
_CACHE_VERSION = 1


def invalidate_seed_edge_cache(repo_path: Any) -> None:
    """Remove a snapshot before an update can change the authoritative DB.

    If the process exits after the DB commit but before writing a replacement,
    the next update must reload SQL rather than trust a pre-commit snapshot.
    """
    path = Path(repo_path) / ".repowise" / _CACHE_NAME
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("graph_seed_cache_invalidate_failed", path=str(path), error=str(exc))


def _decode(raw: str | None) -> list:
    try:
        value = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _edge_dict(row: tuple) -> dict:
    return {
        "source_node_id": row[0],
        "target_node_id": row[1],
        "edge_type": row[2],
        "confidence": row[3],
        "imported_names": list(row[4]),
        "hint_source": row[5],
        "resolution_origin": row[6],
        "call_lines": list(row[7]),
        "type_only": bool(row[8]),
        "deferred": bool(row[9]),
    }


def _cache_rows(repo_path: Path, graph_builder: Any) -> list[tuple]:
    from repowise.core.ingestion.graph._scoped import (
        CALL_MERGED_STRUCTURAL_TYPE,
        SEEDED_EDGE_TYPES,
    )

    rows = []
    for source, target, data in graph_builder.graph().edges(data=True):
        edge_type = data.get("edge_type", "imports")
        if edge_type not in SEEDED_EDGE_TYPES and not (
            edge_type == CALL_MERGED_STRUCTURAL_TYPE and data.get("resolution_origin")
        ):
            continue
        rows.append(
            (
                source,
                target,
                edge_type,
                data.get("confidence", 1.0),
                tuple(data.get("imported_names", ())),
                data.get("hint_source"),
                data.get("resolution_origin"),
                tuple(data.get("call_lines", ())),
                bool(data.get("type_only", False)),
                bool(data.get("deferred", False)),
            )
        )
    return rows


async def _database_stamp(repo_path: Path) -> tuple[str, int, str] | None:
    from sqlalchemy import func, select

    from repowise.core.persistence import create_engine, create_session_factory, get_session
    from repowise.core.persistence.database import resolve_db_url
    from repowise.core.persistence.models import GraphEdge, Repository

    engine = create_engine(resolve_db_url(repo_path))
    try:
        async with get_session(create_session_factory(engine)) as session:
            repo_id = (
                await session.execute(
                    select(Repository.id).where(
                        Repository.local_path == str(repo_path.resolve())
                    )
                )
            ).scalar_one_or_none()
            if repo_id is None:
                return None
            edge_count = await session.scalar(
                select(func.count(GraphEdge.id)).where(GraphEdge.repository_id == repo_id)
            )
            fingerprint = (
                await session.execute(
                    select(Repository.graph_edges_parser_fingerprint).where(
                        Repository.id == repo_id
                    )
                )
            ).scalar_one_or_none()
            return repo_id, int(edge_count or 0), fingerprint or ""
    finally:
        await engine.dispose()


async def load_seed_edges(repo_path: Any) -> Iterator[dict] | None:
    """Persisted edges of the seeded types, using the local snapshot when valid."""
    from sqlalchemy import and_, or_, select

    from repowise.core.ingestion.graph._scoped import (
        CALL_MERGED_STRUCTURAL_TYPE,
        SEEDED_EDGE_TYPES,
    )
    from repowise.core.persistence import create_engine, create_session_factory, get_session
    from repowise.core.persistence.database import resolve_db_url
    from repowise.core.persistence.models import GraphEdge

    root = Path(repo_path).resolve()
    stamp = await _database_stamp(root)
    if stamp is None or stamp[1] == 0:
        return None
    repo_id, edge_count, fingerprint = stamp
    from repowise.core.ingestion.parse_cache import parser_fingerprint

    # A resolver/schema change can make every persisted edge stale. In that
    # case a patch must not seed old edges, even if the local snapshot matches
    # the database exactly; the full update will heal and re-stamp the store.
    if not fingerprint or fingerprint != parser_fingerprint():
        log.info("graph_seed_cache_skipped", reason="edge parser fingerprint mismatch")
        return None
    cache_path = root / ".repowise" / _CACHE_NAME
    try:
        cache = load_sealed_pickle(cache_path, domain=_CACHE_NAME)
        if (
            cache.get("version") == _CACHE_VERSION
            and cache.get("repo_path") == str(root)
            and cache.get("repo_id") == repo_id
            and cache.get("edge_count") == edge_count
            and cache.get("parser_fingerprint") == fingerprint
        ):
            rows = cache.get("edges")
            if isinstance(rows, list):
                log.info("graph_seed_cache_hit", edges=len(rows))
                return (_edge_dict(row) for row in rows)
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("graph_seed_cache_unavailable", error=str(exc))

    engine = create_engine(resolve_db_url(root))
    try:
        async with get_session(create_session_factory(engine)) as session:
            rows = (
                await session.execute(
                    select(
                        GraphEdge.source_node_id,
                        GraphEdge.target_node_id,
                        GraphEdge.edge_type,
                        GraphEdge.confidence,
                        GraphEdge.imported_names_json,
                        GraphEdge.hint_source,
                        GraphEdge.resolution_origin,
                        GraphEdge.call_lines_json,
                        GraphEdge.type_only,
                        GraphEdge.deferred,
                    ).where(
                        GraphEdge.repository_id == repo_id,
                        or_(
                            GraphEdge.edge_type.in_(sorted(SEEDED_EDGE_TYPES)),
                            and_(
                                GraphEdge.edge_type == CALL_MERGED_STRUCTURAL_TYPE,
                                GraphEdge.resolution_origin.is_not(None),
                            ),
                        ),
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    if not rows:
        return None
    return (
        {
            "source_node_id": r[0],
            "target_node_id": r[1],
            "edge_type": r[2],
            "confidence": r[3],
            "imported_names": _decode(r[4]),
            "hint_source": r[5],
            "resolution_origin": r[6],
            "call_lines": _decode(r[7]),
            "type_only": bool(r[8]),
            "deferred": bool(r[9]),
        }
        for r in rows
    )


async def save_seed_edge_cache(repo_path: Any, graph_builder: Any) -> None:
    """Save a sealed reusable-edge snapshot after the database commit succeeds."""
    from repowise.core.ingestion.parse_cache import parser_fingerprint

    root = Path(repo_path).resolve()
    try:
        stamp = await _database_stamp(root)
        if stamp is None:
            return
        repo_id, edge_count, stored_fingerprint = stamp
        payload = {
            "version": _CACHE_VERSION,
            "repo_path": str(root),
            "repo_id": repo_id,
            "edge_count": edge_count,
            "parser_fingerprint": stored_fingerprint or parser_fingerprint(),
            "edges": _cache_rows(root, graph_builder),
        }
        dump_sealed_pickle(root / ".repowise" / _CACHE_NAME, payload, domain=_CACHE_NAME)
        log.info("graph_seed_cache_saved", edges=len(payload["edges"]))
    except Exception as exc:
        # The cache is optional; the next update can reload its seed from SQL.
        log.warning("graph_seed_cache_save_failed", error=str(exc))
