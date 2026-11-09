"""Weekly CompanyFiles -> kg ingest (docs/ingestion-plan.md). Entry point:
python -m ai.file_ingest.run --help"""

# kg must finish importing before ai.airtable_ingest.ingest: kg.export imports
# ingest, and ingest imports kg, so importing ingest first is circular.
import kg  # noqa: F401,E402
