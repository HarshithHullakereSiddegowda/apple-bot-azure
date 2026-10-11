"""HUMAN action: record a bid / no-bid decision (stand-in for the approval button / Teams card / Power Automate).

    python -m tender.decide T-009 bid --by "bid-manager" --note "ISO 9001 certificate confirmed on file"
"""
import argparse
import json

from tender.tools import record_bid_decision

if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("tender_id")
    ap.add_argument("decision", choices=["bid", "no_bid"])
    ap.add_argument("--by", required=True, help="the person deciding")
    ap.add_argument("--note", required=True, help="why (kept in the audit trail)")
    a = ap.parse_args()
    print(json.dumps(record_bid_decision(a.tender_id, a.decision, a.by, a.note), indent=2))
