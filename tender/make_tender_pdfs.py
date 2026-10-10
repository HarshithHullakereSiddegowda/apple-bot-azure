"""Generate FICTIONAL tender documents (PDF, with tables) for testing the Tender Copilot.

Real tender documents sit behind portal logins with usage conditions, so the test set is synthetic but structured
like a Commonwealth request for tender: key dates, conditions for participation, scope, evaluation criteria
with weightings, response format and a pricing schedule.

    python tender/make_tender_pdfs.py      # writes tender/data/docs/T-001.pdf and T-009.pdf
"""
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).resolve().parent / "data" / "docs"
STYLES = getSampleStyleSheet()

TENDERS = {
    "T-001": {
        "title": "Request for Tender: Technical Publications Services for the Protected Mobility Vehicle Fleet",
        "ref": "CASG/LSD/RFT/2026-118 (FICTIONAL)",
        "agency": "Department of Defence, CASG, Land Systems Division",
        "overview": (
            "The Commonwealth seeks a supplier to update, restructure and convert the maintenance publications of the "
            "protected mobility vehicle fleet to the S1000D specification under DEF(AUST) IPS-5630. Legacy publications "
            "exist as approximately 3,400 pages of PDF and FrameMaker source. Work is delivered through task orders "
            "over 24 months, with an option to extend by 12 months."
        ),
        "dates": [
            ("RFT release", "16 October 2026"),
            ("Industry briefing (online)", "28 October 2026, 10:00 AEDT"),
            ("Last date for questions", "6 November 2026"),
            ("Closing time", "20 November 2026, 12:00 noon AEDT"),
            ("Expected contract start", "February 2027"),
        ],
        "conditions": [
            ("C1", "Tenderer must be a member of the Defence Industry Security Program (DISP) at Entry Level or above."),
            ("C2", "All personnel performing the work must hold AGSVA Baseline security clearance at contract start."),
            ("C3", "Tenderer must demonstrate delivery of at least one S1000D publications project in the last 5 years."),
            ("C4", "Tenderer must hold professional indemnity insurance of at least AUD 5 million."),
        ],
        "scope": [
            "Convert legacy maintenance publications to S1000D data modules (Issue 4.1) using the Commonwealth CSDB.",
            "Develop Repair Parts Illustrated Listings (RPIL) and Repair Parts Scales (RPS) for 3 vehicle variants.",
            "Produce technical illustrations, including exploded views, to DEF(AUST) 5629C graphics standards.",
            "Deliver an ASDEFCON publications package (DID-ILS-TDATA-PUBPACK) for each variant.",
            "Provide an 80% review-ready draft for Commonwealth review before each final delivery.",
        ],
        "criteria": [
            ("1", "Capability and experience in S1000D technical publications", "35%"),
            ("2", "Proposed personnel, clearances and availability", "25%"),
            ("3", "Methodology, quality assurance and review approach", "20%"),
            ("4", "Australian Industry Capability and workforce development", "10%"),
            ("5", "Value for money", "10%"),
        ],
        "format": [
            ("Part A", "Compliance with conditions for participation (C1 to C4)", "2 pages"),
            ("Part B", "Response to evaluation criteria 1 to 4", "15 pages"),
            ("Part C", "CVs of proposed personnel", "2 pages per person"),
            ("Part D", "Pricing schedule (Attachment 1)", "No limit"),
        ],
        "pricing": [
            ("Senior technical writer (S1000D)", "Day rate", "____"),
            ("Technical writer", "Day rate", "____"),
            ("Technical illustrator", "Day rate", "____"),
            ("Publications project manager", "Day rate", "____"),
        ],
    },
    "T-009": {
        "title": "Request for Tender: Integrated Logistics Support for Maritime Mine Countermeasures",
        "ref": "CASG/MSD/RFT/2026-207 (FICTIONAL)",
        "agency": "Department of Defence, CASG, Maritime Systems Division",
        "overview": (
            "The Commonwealth requires integrated logistics support management for a maritime mine countermeasures "
            "capability entering service in 2027. Services include logistics support analysis, codification of new "
            "items of supply, inventory optimisation and support to introduction into service. Initial term 18 months."
        ),
        "dates": [
            ("RFT release", "20 October 2026"),
            ("Last date for questions", "20 November 2026"),
            ("Closing time", "4 December 2026, 2:00 pm AEDT"),
            ("Expected contract start", "March 2027"),
        ],
        "conditions": [
            ("C1", "Tenderer must hold current ISO 9001 quality management certification from an accredited body."),
            ("C2", "Personnel must hold AGSVA Baseline security clearance."),
            ("C3", "Tenderer must have delivered ILS services on at least one Defence maritime program in the last 7 years."),
        ],
        "scope": [
            "Develop and maintain the Integrated Logistics Support Plan and Logistics Support Analysis records.",
            "Codify new items of supply in accordance with NATO codification procedures.",
            "Model inventory and recommend initial spares holdings across two operating bases.",
            "Support introduction into service, including provisioning conferences and acceptance activities.",
        ],
        "criteria": [
            ("1", "ILS and LSA capability on Defence maritime programs", "40%"),
            ("2", "Proposed personnel and availability", "30%"),
            ("3", "Methodology and risk management", "20%"),
            ("4", "Value for money", "10%"),
        ],
        "format": [
            ("Part A", "Compliance with conditions for participation (C1 to C3), including ISO 9001 certificate", "2 pages"),
            ("Part B", "Response to evaluation criteria 1 to 3", "12 pages"),
            ("Part C", "Pricing schedule (Attachment 1)", "No limit"),
        ],
        "pricing": [
            ("ILS manager", "Day rate", "____"),
            ("Logistics support analyst", "Day rate", "____"),
            ("Codification specialist", "Day rate", "____"),
        ],
    },
}


def table(rows, header, widths):
    t = Table([header, *rows], colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    return t


def wrap(rows):
    return [[Paragraph(str(c), STYLES["BodyText"]) for c in r] for r in rows]


def build(tender_id: str, t: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{tender_id}.pdf"
    h1, h2, body = STYLES["Title"], STYLES["Heading2"], STYLES["BodyText"]
    s = [
        Paragraph(t["title"], h1),
        Paragraph(f"<b>Reference:</b> {t['ref']}<br/><b>Agency:</b> {t['agency']}", body),
        Paragraph("<b>FICTIONAL DOCUMENT FOR SOFTWARE TESTING. NOT A REAL TENDER.</b>", body),
        Spacer(1, 6 * mm),
        Paragraph("1. Overview", h2), Paragraph(t["overview"], body),
        Paragraph("2. Key dates", h2), table(wrap(t["dates"]), ["Milestone", "Date"], [70 * mm, 90 * mm]),
        Paragraph("3. Conditions for participation (mandatory)", h2),
        Paragraph("A tender that does not meet every condition below will be excluded from evaluation.", body),
        table(wrap(t["conditions"]), ["ID", "Condition"], [15 * mm, 145 * mm]),
        PageBreak(),
        Paragraph("4. Scope of services", h2),
        *[Paragraph(f"• {item}", body) for item in t["scope"]],
        Paragraph("5. Evaluation criteria", h2),
        table(wrap(t["criteria"]), ["#", "Criterion", "Weighting"], [10 * mm, 125 * mm, 25 * mm]),
        Paragraph("6. Response format", h2),
        table(wrap(t["format"]), ["Part", "Content", "Page limit"], [20 * mm, 110 * mm, 30 * mm]),
        Paragraph("7. Pricing schedule (Attachment 1)", h2),
        table(wrap(t["pricing"]), ["Role", "Unit", "Rate (AUD, excl. GST)"], [80 * mm, 30 * mm, 50 * mm]),
    ]
    SimpleDocTemplate(str(path), pagesize=A4, title=t["title"]).build(s)
    return path


if __name__ == "__main__":
    for tid, spec in TENDERS.items():
        print("wrote", build(tid, spec))
