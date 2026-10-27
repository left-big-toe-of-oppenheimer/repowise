"""A patched update writes only the re-resolved nodes' structure, never metrics.

A patched graph carries no fresh centrality, so ``persist_scoped_graph_nodes``
must update the structural columns of the nodes it re-resolved (a body edit
moves the lines of everything below it) while leaving every metric column, and
every node outside the scope, exactly as the last full index stored them.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from repowise.core.persistence.models import GraphNode
from repowise.core.pipeline.incremental import build_repo_graph
from repowise.core.pipeline.persist import persist_graph_nodes, persist_scoped_graph_nodes
from tests.unit.persistence.helpers import insert_repo

_SERVICE = "src/com/acme/svc/Service.java"
_V1 = """package com.acme.svc;

public class Service {
    public String first() {
        return "a";
    }

    public String second() {
        return "b";
    }
}
"""
_V2 = _V1.replace('return "a";', '// moved\n        String x = "a";\n        return x;')
_OTHER = "src/com/acme/svc/Other.java"


async def _nodes(session, repo_id: str) -> dict[str, GraphNode]:
    rows = (
        (await session.execute(select(GraphNode).where(GraphNode.repository_id == repo_id)))
        .scalars()
        .all()
    )
    return {row.node_id: row for row in rows}


async def test_scoped_persist_updates_structure_and_keeps_metrics(async_session, tmp_path: Path):
    repo = await insert_repo(async_session)
    for rel, text in {
        _SERVICE: _V1,
        _OTHER: "package com.acme.svc;\npublic class Other {}\n",
    }.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    first = build_repo_graph(tmp_path, [], collect_sources=True)[2]
    await persist_graph_nodes(async_session, repo.id, first)
    # Distinguishable metric values, as if a full index had computed them.
    for row in (await _nodes(async_session, repo.id)).values():
        row.pagerank = 0.123
        row.community_id = 7
    await async_session.commit()
    before = {n: (r.start_line, r.end_line) for n, r in (await _nodes(async_session, repo.id)).items()}

    (tmp_path / _SERVICE).write_text(_V2)
    second = build_repo_graph(tmp_path, [], collect_sources=True)[2]
    second_symbol = f"{_SERVICE}::Service::second"
    await persist_scoped_graph_nodes(async_session, repo.id, second, (_SERVICE,))
    await async_session.commit()

    after = await _nodes(async_session, repo.id)
    # The edit shifted ``second`` down two lines; the stored row follows.
    assert after[second_symbol].start_line == before[second_symbol][0] + 2
    # Metrics are exactly what the last full index stored, in and out of scope.
    assert {row.pagerank for row in after.values()} == {0.123}
    assert {row.community_id for row in after.values()} == {7}
    # A node outside the scope was not rewritten.
    assert (after[_OTHER].start_line, after[_OTHER].end_line) == before[_OTHER]
