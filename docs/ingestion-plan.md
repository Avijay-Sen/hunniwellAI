# Weekly ingestion, history & MCP interface — plan for review

## The idea in short
Supabase should hold everything Hunniwell knows about each company — meeting notes, decks, spreadsheets, web research — and stay current automatically.

1. **Weekly job** reads the OneDrive-synced `CompanyFiles` folder, detects new/changed/deleted files by hash, extracts text, and writes structured claims plus searchable text chunks to Supabase.
2. **Every row is tagged** with the company id, the source file, an ingest run, and a **period** (event or week), so history is queryable. When a file changes, its old claims are marked superseded, not deleted.
3. **Schema changes** are versioned SQL migrations in the repo (new tables: `periods`, `ingest_runs`, `documents`, `company_aliases`; new columns on `sources`/`claims`; RLS on). The current `upsert_source` duplicates claims on every re-run, so fixing that comes first.
4. **Interface**: an MCP server over the existing `kg` functions (search, company profile, company history) usable from Claude/ChatGPT; a ChatGPT-style chat UI with saved conversations comes later.
5. **Timeline**: ~4 weeks to a live weekly job, MCP server in weeks 5–6, optional chat UI after.

**Please review:** the "Open questions for reviewers" section near the end. Nothing here is implemented yet.

---

# Plan: Weekly OneDrive → Supabase ingestion, history, and an MCP/chat interface

## Context
Hunniwell wants Supabase to hold everything known about each company (meeting notes, decks, spreadsheets, web research), refreshed weekly from the OneDrive `CompanyFiles` tree, with every row tagged to a company id and a time-frame so history is queryable. End goal: query it from ChatGPT/Claude via an MCP server, later with a chat UI that keeps conversation logs.

**Status: DRAFT for review — nothing below is implemented yet.** Please comment on the "Open questions" section at the bottom in particular.

Decisions already made: files come from a **OneDrive-synced folder** on a machine; interface = **MCP server first**; history = **`periods` table + `period_id`**; file types = **pptx, pdf, docx/md/txt, xlsx/csv** (Zoom recordings skipped for now — too large).

## What already exists (reuse, don't rebuild)
- `kg/` — `resolve_company` (`kg/companies.py`, idempotent upsert on `name_norm`), `write_claims`/`enrich` (`kg/claims.py`, enrich supersedes atomically), `semantic_search` (`kg/search.py`), `resolve_citation` (`kg/citations.py`), embeddings (`kg/embeddings.py`, local mxbai 1024-dim), `apply_schema` (`kg/config.py`).
- `ai/airtable_ingest/ingest.py` — tree walking (`iter_event_companies`, `events.py` `EVENT_LAYOUTS`) and extractors `extract_pdf/docx/pptx/text_file`. **No xlsx extractor.**
- `kg/web_adapter.py` `ingest_memo` — parses cited web-research memos into claims; **not wired to anything yet**.
- `ai/kg_query/tool.py` `ask()` — retrieval + cited answer (PR #9).
- `.mcp.json` already points at the hosted Supabase MCP for project `dkimzgobukqqrflmprrp`, but `.claude/settings.local.json` disables it.

## Gaps found (these drive the work)
1. `upsert_source` always inserts a new row, and `claims_idem` includes `source_id` → **a weekly re-run duplicates every claim.**
2. No time model: claims only have `created_at` (ingest time). No period/as-of, no way to pass `captured_at`.
3. No file-level record: change detection is folder-level JSON state files (`.processed.json`, `kg_migration_state.json`) that won't survive a scheduler; a failed KG write still marks a company processed.
4. Extraction yields only 23 FIELD_MAP fields per company — meeting-note detail is lost. No chunk-level text for retrieval.
5. No migration tooling: `schema.sql` is `create ... if not exists` only, can't alter tables or rebuild `claims_idem`.
6. No scheduler, no xlsx, no Zoom/transcripts, RLS not enabled on any table (Supabase exposes `public` via PostgREST).

## Connecting Claude to Supabase for schema changes
Three options; recommended combination:
- **Migrations as SQL files in the repo** (`kg/migrations/NNN_*.sql`) + a small runner (extend `scripts/kg_apply_schema.py`) that records applied versions in a `schema_migrations` table and runs over the existing `SUPABASE_DB_URL` connection (already verified working). Reviewable in PRs, repeatable on local pgvector for tests first.
- **Enable the Supabase MCP** (already in `.mcp.json`): in Claude Code run `/mcp`, enable `supabase`, complete the browser OAuth. Gives me `list_tables`, `execute_sql`, `apply_migration`, and security/performance advisors. Use it mainly for inspection and RLS/advisor checks; keep it scoped to this project.
- Before any schema change on the live project: take a backup (or create a Supabase branch) and rehearse the migration on local pgvector.
- Housekeeping: rotate the database password before go-live, keep credentials only in the gitignored `.env`, and use a dedicated read-only role for the MCP server later.

## Schema changes (migration 001+)
- `periods(id, label unique, kind {event|weekly|historical}, starts_on, ends_on)` — event periods derived from the folder date suffix (e.g. `JPM 2026 (260115)`); weekly runs get `2026-W45`.
- `ingest_runs(id, started_at, finished_at, status, stats jsonb, host)` — replaces JSON state files; every row records the run that wrote it.
- `documents(id, company_id, relpath unique, sha256, size, mtime, file_kind, period_id, status {active|deleted|skipped_large|error}, first_seen_run, last_seen_run, extractor_version)` — the change-detection registry.
- `sources` += `document_id`, `period_id`; unique index on `(document_id)` / `(company_id, kind, uri)`; make `upsert_source` a real upsert and accept `captured_at`.
- `claims` += `period_id`, `run_id`, `as_of date`; **rebuild `claims_idem`** to include `period_id` and mirror it in the `ON CONFLICT` target in `_insert_claims`.
- Companies: add human-friendly `company_code` (identity column) and `company_aliases(company_id, alias_norm unique)`; unmatched/ambiguous folder names go to `ingest_unmatched` for review instead of silently creating duplicates.
- Later (phase 5): `conversations`, `messages(role, content, citations jsonb)` for chat logs.
- Enable RLS on all tables (no anon policies); the ingest and MCP roles connect directly.

## Ingestion pipeline (`ai/file_ingest/`, new)
1. **Scan** the synced root (reuse `iter_event_companies`); hash + stat each file; compare to `documents` → new / changed / unchanged / deleted. Files over a size cap or video/audio extensions are recorded as `skipped_large` (visible, not ingested).
2. **Extract text** — move/reuse the extractors from `ingest.py` into a shared module; add `.xlsx/.csv` (add `openpyxl`). Note: scanned PDFs have no OCR (flag, don't fail).
3. **Claims, two layers per file**: (a) structured field claims using the existing FIELD_MAP vocabulary via a per-file LLM call; (b) chunk claims (`field='doc_chunk'`, ~500–800 tokens) so meeting notes are searchable. Source = the file (`uri` = relpath, kind from file type; meeting notes → `internal_notes`, decks → `pitch_deck`).
4. **Write per file in one transaction**: idempotent source upsert → supersede claims from the previous version of that document (history preserved with its old `period_id`) → `write_claims`. One bad file never aborts the run; failures are retried next week because state lives in the DB.
5. **Web memos**: wire `web_research.txt` through `kg/web_adapter.ingest_memo`.
6. **Backfill**: Airtable (`scripts/migrate_to_kg.py`) and historical events get `historical`/`event` periods; run once on a Supabase branch first.
7. **Schedule**: weekly `launchd` (or cron) job on the machine that holds the synced folder, with OneDrive set to "Always keep on this device" for `CompanyFiles` (Files On-Demand placeholders would break reads). Failure/summary notification (email or Slack webhook). GitHub Actions cannot see the folder.

## MCP server (`ai/kg_mcp/`, new)
Python `mcp` SDK (FastMCP) over the existing `kg` functions, read-only DB role. Tools: `search_claims(query, k, company, period)`, `get_company(name|id)`, `company_history(company, field)` (includes superseded + periods), `list_companies`, `ask(question)` (wraps `ai/kg_query`). Extend `kg/search.py` with `period_id` / `include_superseded` filters. Transports: stdio (Claude Desktop/Code) first; remote streamable-HTTP with auth for ChatGPT connectors (ChatGPT expects `search`/`fetch`-style tools — verify against current OpenAI docs when building). Confidential company data is exposed here, so auth is mandatory before any public URL.

## Timeline (rough, one developer pairing with Claude)
| When | Milestone |
|---|---|
| Days 1–2 | Decisions, rotate password, backup/branch, enable Supabase MCP, migration runner |
| Week 1 | Schema migrations + idempotent `upsert_source` + `claims_idem` rebuild + tests on local pgvector |
| Week 2 | Scanner, `documents` registry, xlsx extractor, per-file extraction (fields + chunks), dry-run on one event |
| Week 3 | Historical backfill + web memos on a Supabase branch; QA with `ai.kg_query`; LLM cost estimate from file counts |
| Week 4 | Scheduler, logging/alerts, first live weekly run, runbook |
| Weeks 5–6 | MCP server (stdio → remote + auth), connect Claude/ChatGPT |
| Weeks 7–9 (optional) | Chat UI with saved conversations, reusing the MCP tools |

## Risks
- LLM extraction cost/time on first full run (count files first; Haiku per file).
- Company-name mismatches between folders → aliases + review queue.
- English-only mxbai embeddings; changing models means re-embedding everything.
- Confidential data exposure via RLS-off tables or an unauthenticated MCP endpoint.
- OneDrive placeholders / laptop asleep at run time.

## Open questions for reviewers
1. **Company identity**: is `name_norm` (lowercased, legal suffixes stripped) enough to match OneDrive folder names to companies, or do we need a curated company list / external id (e.g. Airtable record id) as the source of truth?
2. **Period semantics**: should a claim's `period_id` be the event the document came from, the week it was ingested, or both (current proposal: event period as `period_id`, ingest week via `run_id`)?
3. **Superseding**: when a file changes, we supersede all claims from its previous version. Is that right for decks that get appended to, or should we diff at claim level?
4. **Chunk claims**: storing raw `doc_chunk` text roughly multiplies row count and embedding cost. Acceptable, or limit to meeting notes?
5. **Embedding model**: mxbai is English-only. Any non-English documents in the tree?
6. **Hosting**: which machine will run the weekly job, and is it reliably on/awake? If not, do we accept a small always-on server with a one-way OneDrive sync instead?
7. **Access control for the MCP server**: who should be able to query it (just the team vs. external partners), and do any companies/files need to be excluded?
8. **Zoom recordings**: skip entirely, or ingest the small auto-generated transcripts (`.vtt`) later?
9. **Cost ceiling** for the first full LLM extraction run (needs a file count per event first).

## Verification
- Local pgvector: `pytest tests/kg tests/kg_query` plus new tests for upsert idempotency, period tagging, supersede-on-change, and the rebuilt `claims_idem`.
- Run ingest twice on one event: second run must report 0 new sources/claims; modify one file and confirm only its claims are superseded and the old rows keep their `period_id`.
- Supabase advisors (via MCP) show RLS enabled and no warnings; `ai.kg_query.cli` answers a history question ("what was X's round size in 2025 vs now?") with verified citations.
- MCP: connect from Claude Code/Desktop, call each tool; for ChatGPT, test the remote endpoint with auth on and off.
