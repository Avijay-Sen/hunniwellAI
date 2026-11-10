#!/usr/bin/env python3
"""Generate a SYNTHETIC CompanyFiles tree for demoing ai.file_ingest.

Every company here is fictional and marked "(DEMO)"; every file says it is
synthetic. Layout matches the real OneDrive tree (<Event (YYMMDD)>/<Company>/...)
and covers each supported type: pptx, pdf, docx, md, xlsx, csv, plus an mp4
that the ingest records but skips.

    python scripts/make_demo_files.py                      # -> ~/Documents/Hunniwell-demo/CompanyFiles
    python scripts/make_demo_files.py --out /some/dir
    python scripts/make_demo_files.py --apply-update       # change one file, to demo supersede/history
"""
from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Pt

BANNER = "SYNTHETIC DEMO DATA — fictional company, not a real investment record."
EVENT_1 = "DEMO Pitch Day (261020)"
EVENT_2 = "DEMO Follow-up Review (261105)"


# ---- writers --------------------------------------------------------------------

def write_pptx(path: Path, slides: list[tuple[str, list[str], str]]) -> None:
    prs = Presentation()
    for title, bullets, notes in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = title
        body = slide.placeholders[1].text_frame
        body.text = bullets[0]
        for b in bullets[1:]:
            p = body.add_paragraph()
            p.text = b
            p.font.size = Pt(18)
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
    prs.save(path)


def write_docx(path: Path, title: str, paragraphs: list[str], table: list[list[str]] | None = None) -> None:
    doc = Document()
    doc.add_heading(title, level=1)
    for p in paragraphs:
        doc.add_paragraph(p)
    if table:
        t = doc.add_table(rows=0, cols=len(table[0]))
        for row in table:
            cells = t.add_row().cells
            for c, v in zip(cells, row):
                c.text = v
    doc.save(path)


def write_pdf(path: Path, lines: list[str]) -> None:
    """Minimal one-page text PDF (Helvetica), readable by pypdf."""
    def esc(s: str) -> str:
        s = s.encode("latin-1", "replace").decode("latin-1")
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    stream = "BT /F1 11 Tf 50 760 Td 15 TL\n" + "\n".join(f"({esc(l)}) Tj T*" for l in lines) + "\nET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def write_xlsx(path: Path, sheets: dict[str, list[list]]) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)


def write_csv(path: Path, rows: list[list]) -> None:
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


# ---- content --------------------------------------------------------------------

GLUCO_NOTES = f"""# Meeting notes — GlucoSense Labs (DEMO)
{BANNER}

Date: 2026-10-20 (pitch day follow-up call, 45 min)
Attendees: CEO Dana Whitfield, CTO Marcus Oyelaran; HLV: deal team

## Key points
- Raising a $6M seed round; $1.5M soft-circled from Northwind Health Ventures (fictional).
- Needle-free glucose patch reads interstitial fluid via microneedle-free reverse iontophoresis.
- Pilot (n=42, type 2 diabetes) showed MARD of 9.8% vs. fingerstick reference.
- Plans FDA De Novo submission in Q2 2027; no predicate device identified yet.

## Concerns
- Skin irritation reported in 3 of 42 pilot participants.
- Reimbursement path unclear; CEO expects CPT 95249 coverage but has no payer conversations yet.
- Manufacturing partner not yet selected.

## Next steps
- Request full pilot data set and IRB approval letter.
- Intro to our CGM clinical advisor.
"""

GLUCO_NOTES_UPDATE = f"""# Meeting notes — GlucoSense Labs (DEMO)
{BANNER}

Date: 2026-11-04 (second diligence call, 60 min)
Attendees: CEO Dana Whitfield, CFO Priya Raman; HLV: deal team

## Key points
- Seed round increased to $8M after strong demand; Northwind Health Ventures now leading with $3M.
- Expanded pilot (n=118, type 1 and type 2 diabetes) showed MARD of 8.9% vs. fingerstick reference.
- Signed manufacturing agreement with a contract manufacturer in Penang (fictional).
- Pre-submission meeting with FDA scheduled for January 2027.

## Concerns
- Skin irritation rate fell to 2.5% (3 of 118) after adhesive change.
- Still no payer conversations on reimbursement.

## Next steps
- Reference calls with two pilot endocrinologists.
- Draft term sheet for IC review.
"""


def build(out: Path) -> Path:
    root = out
    if root.exists():
        shutil.rmtree(root)

    g = root / EVENT_1 / "GlucoSense Labs (DEMO)"
    c = root / EVENT_1 / "CardioLoop Medical (DEMO)"
    o = root / EVENT_1 / "OrthoFlex Robotics (DEMO)"
    g2 = root / EVENT_2 / "GlucoSense Labs (DEMO)"
    for d in (g, c, o, g2):
        d.mkdir(parents=True)

    # GlucoSense: deck, meeting notes, cap table
    write_pptx(g / "GlucoSense Pitch Deck.pptx", [
        ("GlucoSense Labs (DEMO)", [BANNER, "Needle-free continuous glucose monitoring patch",
                                    "HQ: Austin, Texas, USA"], "Presenter: Dana Whitfield, CEO"),
        ("Problem", ["38M Americans live with diabetes", "Fingersticks are painful; adherence drops below 50% at 6 months"], ""),
        ("Solution", ["7-day wearable patch, no needles", "Reverse iontophoresis reads interstitial glucose",
                      "Smartphone app with trend alerts"], ""),
        ("Clinical data", ["Pilot n=42 type 2 diabetes patients", "MARD 9.8% vs. fingerstick reference",
                           "Skin irritation in 3 of 42 participants"], "Full dataset available on request"),
        ("Regulatory & raise", ["Class II, FDA De Novo planned Q2 2027", "Raising $6M seed",
                                "Use of funds: pivotal study, manufacturing scale-up"], ""),
    ])
    (g / "Meeting notes - 2026-10-20.md").write_text(GLUCO_NOTES)
    write_xlsx(g / "Cap Table.xlsx", {
        "Cap table": [["SYNTHETIC DEMO DATA"], ["Holder", "Shares", "% fully diluted"],
                      ["Founders", 6_000_000, "60%"], ["Angel investors", 1_500_000, "15%"],
                      ["Option pool", 2_500_000, "25%"]],
        "Round": [["Round", "Target ($M)", "Committed ($M)", "Lead"],
                  ["Seed", 6.0, 1.5, "Northwind Health Ventures (fictional)"]],
    })

    # CardioLoop: one-pager PDF, diligence notes docx, recorded pitch (skipped)
    write_pdf(c / "CardioLoop One-Pager.pdf", [
        "CardioLoop Medical (DEMO) - Company One-Pager",
        BANNER,
        "",
        "Product: insertable cardiac loop recorder with on-device AI arrhythmia detection.",
        "Indication: cryptogenic stroke and unexplained syncope; atrial fibrillation detection.",
        "Medical field: Cardiology, Electrophysiology.",
        "Regulatory: FDA 510(k) cleared in 2025 (predicate: an existing insertable loop recorder).",
        "Clinical: AF detection sensitivity 96.1% and specificity 98.3% (n=310) in a multicenter study.",
        "False-positive alerts reduced 71% vs. predicate device in the same cohort.",
        "Commercial: 14 US hospital systems; 2025 revenue $2.1M.",
        "Raise: $15M Series A, est. close 03/2027.",
        "HQ: Minneapolis, Minnesota, USA. CEO: Robert Lindqvist.",
    ])
    write_docx(c / "Diligence call notes.docx", "Diligence call notes - CardioLoop Medical (DEMO)", [
        BANNER,
        "Call date: 2026-10-21. Spoke with CEO Robert Lindqvist and VP Clinical Hannah Osei.",
        "Strong reimbursement position: existing CPT codes for loop recorder implantation and remote monitoring.",
        "Concern: battery life is 2.5 years versus 3+ years for leading competitors.",
        "Concern: sales cycle at hospital systems averages 9 months.",
        "Positive: two electrophysiologists we spoke with prefer the lower false-alert burden.",
    ], table=[["Metric", "Value"], ["Hospitals live", "14"], ["2025 revenue", "$2.1M"],
              ["Gross margin", "64%"], ["Monthly burn", "$480K"]])
    (c / "Pitch recording.mp4").write_bytes(b"\x00" * 2048)  # stands in for a Zoom recording

    # OrthoFlex: deck + financials csv
    write_pptx(o / "OrthoFlex Investor Deck.pptx", [
        ("OrthoFlex Robotics (DEMO)", [BANNER, "Handheld robotic assistant for knee replacement surgery",
                                       "HQ: Boston, Massachusetts, USA"], ""),
        ("Market", ["~800K total knee replacements per year in the US",
                    "Large robots cost $1M+ and need dedicated OR space"], ""),
        ("Product", ["Handheld robot, $150K list price", "Cut accuracy within 0.5 mm in cadaver lab (n=24 knees)"], ""),
        ("Status", ["Prototype stage; 510(k) submission targeted Q4 2027",
                    "Raising $4M pre-seed; $0.8M committed"], "CEO: Elena Marchetti"),
    ])
    write_csv(o / "Financial projections.csv", [
        ["SYNTHETIC DEMO DATA", "", "", ""],
        ["Metric", "2026", "2027", "2028"],
        ["Units sold", "0", "6", "40"],
        ["Revenue ($M)", "0.0", "0.9", "6.0"],
        ["Net burn ($M)", "1.8", "3.2", "2.5"],
    ])

    # Second, later event: GlucoSense again -> history across periods
    write_pptx(g2 / "GlucoSense Update Deck.pptx", [
        ("GlucoSense Labs (DEMO) — November update", [BANNER, "Seed round now $8M, led by Northwind Health Ventures"], ""),
        ("Progress since pitch day", ["Expanded pilot n=118: MARD 8.9%", "Contract manufacturer signed",
                                      "FDA pre-submission meeting January 2027"], ""),
    ])
    return root


def apply_update(out: Path) -> Path:
    notes = out / EVENT_1 / "GlucoSense Labs (DEMO)" / "Meeting notes - 2026-10-20.md"
    if not notes.exists():
        raise SystemExit(f"run without --apply-update first; missing {notes}")
    notes.write_text(GLUCO_NOTES_UPDATE)
    return notes


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--out", default=str(Path.home() / "Documents" / "Hunniwell-demo" / "CompanyFiles"))
    ap.add_argument("--apply-update", action="store_true",
                    help="Rewrite one meeting-notes file (newer facts) to demo change detection.")
    args = ap.parse_args()
    out = Path(args.out).expanduser()
    if args.apply_update:
        print("Updated:", apply_update(out))
        return
    root = build(out)
    files = sorted(p for p in root.rglob("*") if p.is_file())
    print(f"Wrote {len(files)} synthetic files under {root}")
    for p in files:
        print("  ", p.relative_to(root))


if __name__ == "__main__":
    main()
