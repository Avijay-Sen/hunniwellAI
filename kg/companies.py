import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

import psycopg

_LEGAL_SUFFIX_RE = re.compile(
    r"[\s,\.]+(?:inc|llc|ltd|corp|co)\b\.?$", re.IGNORECASE
)
_WS_RE = re.compile(r"\s+")
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_name(name: str) -> str:
    s = name.strip().lower()
    s = _WS_RE.sub(" ", s)
    # Strip stacked legal suffixes ("Foo Corp, LLC") by applying the regex
    # repeatedly until the string stops changing.
    while True:
        stripped = _LEGAL_SUFFIX_RE.sub("", s)
        if stripped == s:
            break
        s = stripped
    s = _WS_RE.sub(" ", s).strip()
    # Collapse to an alphanumeric-only dedup key so spacing/punctuation variants
    # of the same company merge (e.g. "3D BioFibR Inc" and "3DBioFibR" -> "3dbiofibr").
    return _NONALNUM_RE.sub("", s)


def add_alias(conn: psycopg.Connection, alias: str, company_id: str) -> None:
    """Map another spelling of a company name to an existing company
    (migration 001). Commits."""
    with conn.cursor() as cur:
        cur.execute(
            "insert into company_aliases (alias_norm, company_id) values (%s, %s) "
            "on conflict (alias_norm) do update set company_id = excluded.company_id",
            (_normalize_name(alias), company_id),
        )
    conn.commit()


def resolve_company_with_aliases(conn: psycopg.Connection, relpath: str, name: str, event: str) -> str:
    """resolve_company, but a name listed in company_aliases resolves to the
    aliased company instead of creating a new one (migration 001)."""
    with conn.cursor() as cur:
        cur.execute(
            "select company_id from company_aliases where alias_norm = %s",
            (_normalize_name(name),),
        )
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return resolve_company(conn, relpath, name, event)
        company_id = row[0]
        cur.execute(
            "insert into company_appearances (company_id, event, relpath) values (%s, %s, %s) "
            "on conflict (relpath) do nothing",
            (company_id, event, relpath),
        )
    conn.commit()
    return str(company_id)


def resolve_company(conn: psycopg.Connection, relpath: str, name: str, event: str) -> str:
    name_norm = _normalize_name(name)
    with conn.cursor() as cur:
        # Look up first: an INSERT ... ON CONFLICT consumes an identity value
        # (companies.company_code) even when the company already exists, which
        # leaves gaps in the human-facing company numbers.
        cur.execute(
            "update companies set updated_at = now() where name_norm = %s returning id",
            (name_norm,),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                insert into companies (name_norm, name_raw)
                values (%s, %s)
                on conflict (name_norm) do update set updated_at = now()
                returning id
                """,
                (name_norm, name),
            )
            row = cur.fetchone()
        company_id = row[0]

        cur.execute(
            """
            insert into company_appearances (company_id, event, relpath)
            values (%s, %s, %s)
            on conflict (relpath) do nothing
            """,
            (company_id, event, relpath),
        )
    conn.commit()
    return str(company_id)


# --- Matching for the file ingest (migration 002) ---------------------------
#
# A company folder name resolves when it matches an existing company exactly
# (after _normalize_name) or through company_aliases. A name that matches
# nothing but is CLOSE to an existing company ("Acme Health" vs "Acme Health
# AI", "Glucosense" vs "GlucoSense Bio") is put on the review list
# (ingest_unmatched) and its files are held back, instead of silently creating
# a near-duplicate company. A name close to nothing is a new company.

FUZZY_THRESHOLD = 0.85
_MIN_CONTAIN_LEN = 5  # "acme" inside "acmehealth" counts; "ai" inside anything does not


@dataclass
class CompanyMatch:
    kind: str  # 'exact' | 'alias' | 'approved_new' | 'pending' | 'review' | 'new'
    company_id: Optional[str] = None  # set for exact / alias
    candidate_id: Optional[str] = None  # set for review / pending
    candidate_name: Optional[str] = None
    score: Optional[float] = None


def similarity(a_norm: str, b_norm: str) -> float:
    """0..1 closeness of two normalized names. One name containing the other
    (min length 5) counts as 0.9, enough to need a human decision."""
    if not a_norm or not b_norm:
        return 0.0
    ratio = SequenceMatcher(None, a_norm, b_norm).ratio()
    short, long_ = sorted((a_norm, b_norm), key=len)
    if len(short) >= _MIN_CONTAIN_LEN and short in long_:
        ratio = max(ratio, 0.9)
    return ratio


def match_company(conn: psycopg.Connection, name: str, *, threshold: float = FUZZY_THRESHOLD) -> CompanyMatch:
    """Classify a company folder name against the database. Read-only (safe in
    a dry run). Needs migrations 001 + 002."""
    norm = _normalize_name(name)
    with conn.cursor() as cur:
        cur.execute("select company_id from company_aliases where alias_norm = %s", (norm,))
        row = cur.fetchone()
        if row:
            return CompanyMatch("alias", company_id=str(row[0]))
        cur.execute("select id from companies where name_norm = %s", (norm,))
        row = cur.fetchone()
        if row:
            return CompanyMatch("exact", company_id=str(row[0]))
        cur.execute(
            "select u.status, u.candidate_company_id, c.name_raw, u.score from ingest_unmatched u "
            "left join companies c on c.id = u.candidate_company_id where u.name_norm = %s",
            (norm,),
        )
        row = cur.fetchone()
        if row and row[0] == "new":
            return CompanyMatch("approved_new")
        if row and row[0] == "pending":
            return CompanyMatch("pending", candidate_id=row[1] and str(row[1]),
                                candidate_name=row[2], score=row[3])
        # 'aliased' rows resolve through company_aliases above; if that alias was
        # removed since, fall through and re-check.
        cur.execute("select id, name_norm, name_raw from companies")
        best = None
        for cid, cand_norm, cand_raw in cur.fetchall():
            score = similarity(norm, cand_norm)
            if score >= threshold and (best is None or score > best[0]):
                best = (score, str(cid), cand_raw)
    if best:
        return CompanyMatch("review", candidate_id=best[1], candidate_name=best[2], score=round(best[0], 3))
    return CompanyMatch("new")


def resolve_company_for_ingest(
    conn: psycopg.Connection, relpath: str, name: str, event: str, *, run_id: Optional[str] = None,
) -> tuple[Optional[str], CompanyMatch]:
    """Resolve a company folder for the file ingest. Returns (company_id, match);
    company_id is None when the name is waiting on the review list (it is added
    or refreshed there, and the caller should skip the folder's files).
    Commits."""
    m = match_company(conn, name)
    if m.kind in ("review", "pending"):
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into ingest_unmatched
                    (name_norm, name_raw, example_relpath, candidate_company_id, score,
                     first_seen_run, last_seen_run)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (name_norm) do update set last_seen_run = excluded.last_seen_run
                """,
                (_normalize_name(name), name, relpath, m.candidate_id, m.score, run_id, run_id),
            )
        conn.commit()
        return None, m
    if m.kind == "alias":
        return resolve_company_with_aliases(conn, relpath, name, event), m
    # exact / approved_new / new: create-or-reuse by normalized name.
    return resolve_company(conn, relpath, name, event), m


def list_unmatched(conn: psycopg.Connection, status: Optional[str] = "pending") -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select u.name_norm, u.name_raw, u.example_relpath, u.candidate_company_id,
                   c.name_raw, c.company_code, u.score, u.status
            from ingest_unmatched u left join companies c on c.id = u.candidate_company_id
            where %s::text is null or u.status = %s
            order by u.created_at, u.name_norm
            """,
            (status, status),
        )
        cols = ("name_norm", "name_raw", "example_relpath", "candidate_id",
                "candidate_name", "candidate_code", "score", "status")
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.commit()
    return rows


def approve_alias(conn: psycopg.Connection, name: str, company_id: Optional[str] = None) -> str:
    """Review decision: `name` is another spelling of an existing company
    (the suggested candidate unless company_id is given). Commits."""
    norm = _normalize_name(name)
    with conn.cursor() as cur:
        cur.execute("select candidate_company_id from ingest_unmatched where name_norm = %s", (norm,))
        row = cur.fetchone()
        target = company_id or (row and row[0] and str(row[0]))
        if not target:
            raise ValueError(f"no candidate company for {name!r}; pass the company explicitly")
        cur.execute("select 1 from companies where id = %s", (target,))
        if cur.fetchone() is None:
            raise ValueError(f"company {target} does not exist")
        cur.execute(
            "insert into company_aliases (alias_norm, company_id) values (%s, %s) "
            "on conflict (alias_norm) do update set company_id = excluded.company_id",
            (norm, target),
        )
        cur.execute(
            "update ingest_unmatched set status = 'aliased', candidate_company_id = %s, resolved_at = now() "
            "where name_norm = %s",
            (target, norm),
        )
    conn.commit()
    return str(target)


def approve_new(conn: psycopg.Connection, name: str) -> None:
    """Review decision: `name` is a genuinely new company; the next ingest run
    creates it. Commits."""
    norm = _normalize_name(name)
    with conn.cursor() as cur:
        cur.execute(
            "update ingest_unmatched set status = 'new', resolved_at = now() where name_norm = %s",
            (norm,),
        )
        if cur.rowcount == 0:
            raise ValueError(f"{name!r} is not on the review list")
    conn.commit()
