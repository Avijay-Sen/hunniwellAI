-- 002: period-aware claim dedup + review list for unmatched company names.
--
-- 1. claims_idem now includes period_id. Legacy (Airtable) sources are one per
--    company and are reused across events, so once those claims are tagged
--    with periods the same value at two events must stay two claims. File
--    ingest is unaffected (one source per document, one period per document).
--    The index keeps its name so schema.sql's `create ... if not exists
--    claims_idem` is a no-op afterwards and never recreates the old version.
--    kg/claims.py uses a bare ON CONFLICT DO NOTHING, so it works with either
--    definition.
--
-- 2. ingest_unmatched holds company folder names that are close to, but not
--    exactly, an existing company (e.g. "Acme Health" vs "Acme Health AI").
--    Their files are held back until someone decides with
--    scripts/review_unmatched.py: alias it to the existing company, or mark it
--    as a genuinely new company.
--
-- Idempotent, like 001.

do $$
begin
  if exists (
    select 1 from pg_indexes
    where schemaname = 'public' and indexname = 'claims_idem'
      and indexdef not like '%period_id%'
  ) then
    drop index claims_idem;
  end if;
end $$;

create unique index if not exists claims_idem
  on claims(company_id, coalesce(field, ''), md5(value), source_id, period_id)
  nulls not distinct
  where status = 'active';

create table if not exists ingest_unmatched (
  name_norm text primary key,
  name_raw text not null,
  example_relpath text not null,
  candidate_company_id uuid references companies(id) on delete set null,
  score real,
  status text not null default 'pending'
    check (status in ('pending', 'aliased', 'new')),
  first_seen_run uuid references ingest_runs(id) on delete set null,
  last_seen_run uuid references ingest_runs(id) on delete set null,
  resolved_at timestamptz,
  created_at timestamptz default now()
);

create index if not exists ingest_unmatched_status on ingest_unmatched(status);

alter table ingest_unmatched enable row level security;
