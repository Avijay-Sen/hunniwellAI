import re

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
