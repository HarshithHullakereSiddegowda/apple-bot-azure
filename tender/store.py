"""Handoff storage shared by all three agents (Blob container `tenders`).

    tenders/{tender_id}/notice.json        written by Agent 1
    tenders/{tender_id}/source/*.pdf       tender documents
    tenders/{tender_id}/status.json        {"status": ..., "history": [...]}
    tenders/{tender_id}/score.json         Agent 2: score_fit
    tenders/{tender_id}/requirements.json  Agent 2: extract_requirements
    tenders/{tender_id}/draft/draft.json   Agent 2: draft_response (structured, for audit and evals)
    tenders/{tender_id}/draft/draft.docx   Agent 2: handed to SharePoint for Agent 3

Keyless: the storage account has shared-key access disabled; DefaultAzureCredential uses `az login` locally and the
managed identity in Azure.
"""
import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient

CONTAINER = "tenders"

# Allowed status flow. Agents may only move a tender forward along these edges.
TRANSITIONS = {
    "new": {"scored"},
    "scored": {"scored", "approved_to_bid", "no_bid"},   # re-scoring allowed (e.g. after an addendum)
    "approved_to_bid": {"drafted"},
    "drafted": {"drafted", "sent_to_sharepoint"},
    "sent_to_sharepoint": {"formatted"},
    "no_bid": set(),
    "formatted": set(),
}


@lru_cache(maxsize=1)
def container() -> ContainerClient:
    account = os.environ["TENDER_STORAGE_ACCOUNT"]
    return ContainerClient(f"https://{account}.blob.core.windows.net", CONTAINER, credential=DefaultAzureCredential())


def _path(tender_id: str, name: str) -> str:
    if not tender_id or "/" in tender_id or ".." in tender_id:
        raise ValueError(f"invalid tender_id: {tender_id!r}")
    return f"{tender_id}/{name}"


# ── JSON and file helpers ─────────────────────────────────────────────────────

def put_json(tender_id: str, name: str, data: dict) -> None:
    container().upload_blob(_path(tender_id, name), json.dumps(data, indent=2), overwrite=True)


def get_json(tender_id: str, name: str) -> dict | None:
    try:
        return json.loads(container().download_blob(_path(tender_id, name)).readall())
    except ResourceNotFoundError:
        return None


def put_file(tender_id: str, name: str, data: bytes) -> None:
    container().upload_blob(_path(tender_id, name), data, overwrite=True)


def get_file(tender_id: str, name: str) -> bytes:
    return container().download_blob(_path(tender_id, name)).readall()


def list_sources(tender_id: str) -> list[str]:
    prefix = _path(tender_id, "source/")
    return [b.name.removeprefix(prefix) for b in container().list_blobs(name_starts_with=prefix)]


def list_tenders() -> list[str]:
    return sorted({b.name.split("/", 1)[0] for b in container().list_blobs() if b.name.endswith("/notice.json")})


# ── Status (with an audit history and guarded transitions) ────────────────────

def get_status(tender_id: str) -> str | None:
    s = get_json(tender_id, "status.json")
    return s["status"] if s else None


def set_status(tender_id: str, new: str, by: str, note: str = "") -> None:
    current = get_json(tender_id, "status.json") or {"status": None, "history": []}
    allowed = TRANSITIONS.get(current["status"], set()) if current["status"] else {"new"}
    if new not in allowed:
        raise ValueError(f"{tender_id}: cannot move from {current['status']!r} to {new!r} (allowed: {sorted(allowed)})")
    current["history"].append({"from": current["status"], "to": new, "by": by, "note": note,
                               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    current["status"] = new
    put_json(tender_id, "status.json", current)


# ── Agent 1's handoff ─────────────────────────────────────────────────────────

def put_tender(notice: dict, pdfs: list[Path] = (), by: str = "agent1") -> str:
    """Drop a new tender in the tray: notice + source documents + status 'new'. Idempotent for unknown tenders."""
    tid = notice["tender_id"]
    put_json(tid, "notice.json", notice)
    for pdf in pdfs:
        put_file(tid, f"source/{Path(pdf).name}", Path(pdf).read_bytes())
    if get_status(tid) is None:
        set_status(tid, "new", by=by, note=f"{len(pdfs)} source document(s)")
    return tid
