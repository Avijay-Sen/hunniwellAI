#!/usr/bin/env bash
# Live demo of the weekly file ingest on SYNTHETIC files, against a LOCAL
# database (hunniwell_demo in the hunni-pg container). Never touches Supabase.
#
#   scripts/demo.sh reset      # fresh demo DB + regenerate fictional files
#   scripts/demo.sh preview    # dry run: what would be ingested
#   scripts/demo.sh ingest     # ingest (run twice to show nothing duplicates)
#   scripts/demo.sh update     # change one meeting-notes file
#   scripts/demo.sh history    # funding facts per company, active vs superseded
#   scripts/demo.sh ask "which companies are working on glucose monitoring?"
set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/.."
PY=.venv/bin/python
DB=postgresql://postgres:postgres@localhost:5432/hunniwell_demo
ROOT="$HOME/Documents/Hunniwell-demo/CompanyFiles"
EVENTS=(--event "DEMO Pitch Day (261020)" --event "DEMO Follow-up Review (261105)")
quiet() { grep -v -e "Warning" -e "Loading weights" -e "warnings.warn" || true; }

need_db() {
  if ! docker exec hunni-pg pg_isready -U postgres >/dev/null 2>&1; then
    echo "Starting local database (colima + hunni-pg)..."
    colima status >/dev/null 2>&1 || colima start
    docker start hunni-pg >/dev/null
    until docker exec hunni-pg pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
  fi
}

case "${1:-}" in
  reset)
    need_db
    docker exec hunni-pg psql -U postgres -qc "drop database if exists hunniwell_demo with (force)" \
      -c "create database hunniwell_demo"
    $PY scripts/kg_apply_schema.py --db-url "$DB" | tail -2
    $PY scripts/make_demo_files.py
    ;;
  preview) need_db; $PY -m ai.file_ingest.run --root "$ROOT" "${EVENTS[@]}" --db-url "$DB" --dry-run 2>&1 | quiet ;;
  ingest)  need_db; $PY -m ai.file_ingest.run --root "$ROOT" "${EVENTS[@]}" --db-url "$DB" 2>&1 | quiet ;;
  update)  $PY scripts/make_demo_files.py --apply-update ;;
  history)
    need_db
    docker exec hunni-pg psql -U postgres -d hunniwell_demo -c "
      select 'HW-' || lpad(co.company_code::text, 4, '0') as id, co.name_raw as company,
             p.label as period, s.title as file, c.field, c.status, c.value
      from claims c
      join companies co on co.id = c.company_id
      join sources s on s.id = c.source_id
      join periods p on p.id = c.period_id
      where c.field in ('coming_round', 'size_of_round_m', 'equity_raised_m', 'est_close')
      order by co.name_raw, p.starts_on, s.title, c.field, c.status;"
    ;;
  ask)
    need_db; shift
    TEST_DATABASE_URL="$DB" $PY -m ai.kg_query.cli "$@" 2>&1 | quiet
    ;;
  *) sed -n '2,11p' "$0"; exit 1 ;;
esac
