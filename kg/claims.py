from __future__ import annotations

import hashlib
from typing import Optional
from uuid import uuid4

import psycopg
from pgvector.psycopg import Vector

from kg import embeddings  # module-attr import: callers use embeddings.embed(...)
from kg.models import Claim, ClaimInput


def _new_claim_id() -> str:
    return "clm_" + uuid4().hex


def _existing_active_ids(conn, company_id, source_id, claims) -> list:
    """For each claim, the id of an identical ACTIVE claim already stored for
    this (company, source), else None. Lets re-ingests skip embedding."""
    if source_id is None:
        return [None] * len(claims)
    with conn.cursor() as cur:
        cur.execute(
            "select id, coalesce(field, ''), md5(value) from claims "
            "where company_id = %s and source_id = %s and status = 'active'",
            (company_id, source_id),
        )
        existing = {(f, h): cid for cid, f, h in cur.fetchall()}
    if not existing:
        return [None] * len(claims)
    return [existing.get((c.field or "", hashlib.md5(c.value.encode("utf-8")).hexdigest())) for c in claims]


def _insert_claims(conn, company_id, source_id, claims, writer, *, period_id=None, run_id=None, as_of=None):
    """Insert claims idempotently WITHOUT committing. Returns claim ids.

    period_id / run_id / as_of tag NEW rows (migration 001); they are only sent
    when given, so callers that don't pass them work on an un-migrated DB.
    Claims that already exist (active, same company/field/value/source) return
    the existing id without being re-embedded.

    The embed call site is ALWAYS the module attr `embeddings.embed(...)` so a
    single test patch of `kg.embeddings.embed` intercepts every caller. There is
    no `kg.claims.embed` symbol.

    Idempotency is enforced by the partial unique index claims_idem on
    (company_id, coalesce(field,''), md5(value), source_id) where status='active'.
    A re-insert of the same (field, value, source) returns the existing id and
    creates no duplicate row.

    Shared by write_claims (which commits) and enrich (which commits once,
    atomically, after the supersede UPDATE).
    """
    if not claims:
        return []

    known = _existing_active_ids(conn, company_id, source_id, claims)
    to_embed = [c.value for c, k in zip(claims, known) if k is None]
    vec_iter = iter(embeddings.embed(to_embed) if to_embed else [])

    extra_cols = [(col, val) for col, val in (("period_id", period_id), ("run_id", run_id), ("as_of", as_of)) if val is not None]
    extra_names = "".join(f", {col}" for col, _ in extra_cols)
    extra_marks = ", %s" * len(extra_cols)

    out: list[str] = []
    with conn.cursor() as cur:
        for claim, known_id in zip(claims, known):
            if known_id is not None:
                out.append(known_id)
                continue
            vec = next(vec_iter)
            new_id = _new_claim_id()
            cur.execute(
                # The ON CONFLICT target+predicate below MUST mirror the schema's
                # claims_idem partial-index expression
                #   (company_id, coalesce(field,''), md5(value), source_id)
                # and predicate  WHERE status = 'active'  EXACTLY. Any divergence
                # makes Postgres raise: "there is no unique or exclusion constraint
                # matching the ON CONFLICT specification".
                f"""
                insert into claims
                    (id, company_id, field, value, source_id, writer, confidence, embedding, status{extra_names})
                values
                    (%s, %s, %s, %s, %s, %s, %s, %s, 'active'{extra_marks})
                on conflict (company_id, coalesce(field,''), md5(value), source_id)
                    where status = 'active'
                do nothing
                returning id
                """,
                (
                    new_id,
                    company_id,
                    claim.field,
                    claim.value,
                    source_id,
                    writer,
                    claim.confidence,
                    Vector(vec),
                    *(val for _, val in extra_cols),
                ),
            )
            row = cur.fetchone()
            if row is not None:
                out.append(row[0])
            else:
                # Conflict: an active row already exists; fetch its id.
                cur.execute(
                    """
                    select id from claims
                    where company_id = %s
                      and coalesce(field, '') = coalesce(%s, '')
                      and md5(value) = md5(%s)
                      and source_id = %s
                      and status = 'active'
                    """,
                    (company_id, claim.field, claim.value, source_id),
                )
                out.append(cur.fetchone()[0])
    return out


def write_claims(conn, company_id, source_id, claims, writer):
    """Insert claims idempotently and commit. See _insert_claims for details.

    On any failure the transaction is rolled back (so the connection is not left
    in an aborted-transaction state) and the error re-raised (mirrors enrich)."""
    try:
        out = _insert_claims(conn, company_id, source_id, claims, writer)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return out


def enrich(
    conn,
    company_id: str,
    source_id: str,
    claims: list[ClaimInput],
    writer: str,
    *,
    supersede_claim_ids: Optional[list[str]] = None,
) -> list[str]:
    """Council write path: same insert as write_claims but writer is namespaced
    'council:<agent>'. ATOMIC: the new claim insert(s) AND the supersede UPDATE
    run in one transaction with a single commit at the end, so a supersede
    failure rolls back the newly inserted claim(s). Sets superseded_by on the
    retired rows to the first new claim id.

    Writer convention: the stored writer is namespaced 'council:<agent>'. Pass
    either a bare agent name ('lucia') or an already-namespaced one
    ('council:lucia'); the prefix is added only if absent (no double-prefix)."""
    council_writer = writer if writer.startswith("council:") else "council:" + writer
    try:
        new_ids = _insert_claims(conn, company_id, source_id, claims, council_writer)

        if supersede_claim_ids:
            if not new_ids:
                raise ValueError("cannot supersede claims without a new claim to point to")
            winner_id = new_ids[0]
            with conn.cursor() as cur:
                # Exclude the just-returned new_ids: the idempotent insert returns
                # an EXISTING id on a claims_idem conflict, so a caller passing that
                # id in supersede_claim_ids would otherwise retire the very claim it
                # just (re)wrote -> silent data loss.
                cur.execute(
                    "update claims set status = 'superseded', superseded_by = %s "
                    "where id = any(%s) and id <> all(%s) and company_id = %s",
                    (winner_id, list(supersede_claim_ids), list(new_ids), company_id),
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return new_ids


def query(
    conn: psycopg.Connection,
    company_id: str,
    fields: Optional[list[str]] = None,
) -> list[Claim]:
    """Return active claims for a company, joining source reliability/uri/kind.

    fields filters claims.field to the given list when provided.
    status='active' only.
    """
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
        where c.company_id = %s
          and c.status = 'active'
    """
    params: list = [company_id]
    if fields is not None:
        sql += " and c.field = any(%s)"
        params.append(list(fields))
    sql += " order by c.created_at asc"

    out: list[Claim] = []
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            (
                cid,
                comp_id,
                field,
                value,
                source_id,
                writer,
                confidence,
                status,
                reliability,
                source_uri,
                source_kind,
            ) = row
            out.append(
                Claim(
                    id=cid,
                    company_id=str(comp_id),
                    field=field,
                    value=value,
                    source_id=str(source_id) if source_id is not None else None,
                    writer=writer,
                    confidence=float(confidence) if confidence is not None else None,
                    status=status,
                    reliability=reliability,
                    source_uri=source_uri,
                    source_kind=source_kind,
                )
            )
    return out
