from typing import Optional

import psycopg
from pgvector.psycopg import Vector

from kg import embeddings  # module-attr import: query is embedded via embeddings.embed(...)
from kg.models import Claim

def semantic_search(
    conn: psycopg.Connection,
    text: str,
    k: int = 10,
    company_id: Optional[str] = None,
    *,
    period_id: Optional[str] = None,
    include_superseded: bool = False,
) -> list[Claim]:
    """Nearest claims to ``text``. Active claims only unless
    ``include_superseded`` (history); ``period_id`` needs migration 001."""
    # REAL BUG FIX: embed via the module attr so the test's
    # monkeypatch.setattr("kg.embeddings.embed", fake) intercepts this call and
    # no Voyage network request is made. Do NOT use `from kg.embeddings import embed`.
    qvec = embeddings.embed([text], input_type="query")[0]

    sql = """
        select
            c.id,
            c.company_id,
            c.field,
            c.value,
            c.source_id,
            c.writer,
            c.confidence,
            c.status,
            s.reliability,
            s.uri,
            s.kind
        from claims c
        left join sources s on s.id = c.source_id
        where c.embedding is not null
    """
    params: list = []
    if not include_superseded:
        sql += " and c.status = 'active'"
    if period_id is not None:
        sql += " and c.period_id = %s"
        params.append(period_id)
    if company_id is not None:
        sql += " and c.company_id = %s"
        params.append(company_id)
    sql += " order by c.embedding <=> %s limit %s"
    params.append(Vector(qvec))
    params.append(k)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        Claim(
            id=r[0],
            company_id=str(r[1]),
            field=r[2],
            value=r[3],
            source_id=str(r[4]) if r[4] is not None else None,
            writer=r[5],
            confidence=r[6],
            status=r[7],
            reliability=r[8],
            source_uri=r[9],
            source_kind=r[10],
        )
        for r in rows
    ]
