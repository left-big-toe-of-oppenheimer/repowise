"""Read the persisted edges a graph patch reuses."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def _decode(raw: str | None) -> list:
    try:
        value = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


async def load_seed_edges(repo_path: Any) -> Iterator[dict] | None:
    """Persisted edges of the seeded types as plain dicts, or ``None`` if there are none.

    Selects bare columns rather than ORM rows: a large repo has over a million
    edges, and hydrating each as an entity costs more than the patch saves.
    """
    from sqlalchemy import and_, or_, select

    from repowise.core.ingestion.graph._scoped import (
        CALL_MERGED_STRUCTURAL_TYPE,
        SEEDED_EDGE_TYPES,
    )
    from repowise.core.persistence import create_engine, create_session_factory, get_session
    from repowise.core.persistence.database import resolve_db_url
    from repowise.core.persistence.models import GraphEdge, Repository

    engine = create_engine(resolve_db_url(Path(repo_path)))
    try:
        async with get_session(create_session_factory(engine)) as session:
            repo_id = (
                await session.execute(
                    select(Repository.id).where(
                        Repository.local_path == str(Path(repo_path).resolve())
                    )
                )
            ).scalar_one_or_none()
            if repo_id is None:
                return None
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
                            # Only the structural links a call resolved onto.
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
