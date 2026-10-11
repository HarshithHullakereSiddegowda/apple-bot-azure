"""Tender Copilot HTTP API (FastAPI router). Mounted by app.py only when TENDER_ENABLED=true.

    GET  /tender/tenders                    tray: status, verdict, score
    GET  /tender/tenders/{id}               one tender + pending AI recommendation
    POST /tender/agent                      one chat turn with the Foundry agent (Brick 5 loop)
    POST /tender/tenders/{id}/decision      HUMAN bid / no-bid decision (Brick 6 lock)

All routes require the API key (attached by app.py). Identity note: with an API key the app cannot verify who
the person is, so the decision card asks for a name. In production the name comes from Entra ID sign-in.
"""
import json
from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from tender import store
from tender.agent import client, ensure_agent, run_turn
from tender.tools import get_tender, list_tenders, record_bid_decision

router = APIRouter(prefix="/tender", tags=["tender"])


@lru_cache(maxsize=1)
def _agent_session():
    """Create/refresh the Foundry agent once per process and reuse it (avoids seconds of setup per message)."""
    c = client()
    return c, ensure_agent(c).id


class ChatRequest(BaseModel):
    message: str = Field(min_length=2, max_length=2000)
    thread_id: str | None = Field(default=None, description="Omit on the first message; reuse afterwards")


class DecisionRequest(BaseModel):
    decision: Literal["bid", "no_bid"]
    decided_by: str = Field(min_length=2, max_length=80, description="The person deciding")
    note: str = Field(min_length=3, max_length=1000, description="Why (kept in the audit trail)")


def _pending(tender_id: str) -> dict | None:
    """The AI's recommendation awaiting a human decision, if any."""
    if store.get_status(tender_id) != "awaiting_decision":
        return None
    return store.get_json(tender_id, "decision_request.json")


@router.get("/tenders")
def tenders(status: str | None = None) -> dict:
    return list_tenders(status)


@router.get("/tenders/{tender_id}")
def tender(tender_id: str) -> dict:
    details = get_tender(tender_id)
    if "error" in details:
        raise HTTPException(404, details["error"])
    return {**details, "pending_decision": _pending(tender_id),
            "decision": store.get_json(tender_id, "decision.json")}


@router.post("/agent")
def agent_chat(req: ChatRequest) -> dict:
    c, agent_id = _agent_session()
    thread_id = req.thread_id or c.threads.create().id
    result = run_turn(c, agent_id, thread_id, req.message, trace=False)
    if not result["answer"]:
        raise HTTPException(502, f"Agent run ended with status {result['status']}: {result['error']}")
    raised = {json.loads(t["arguments"] or "{}").get("tender_id") for t in result["tool_calls"]
              if t["tool"] == "request_bid_decision"}
    decisions = [d for tid in sorted(t for t in raised if t) if (d := _pending(tid))]
    return {"thread_id": thread_id, "answer": result["answer"],
            "tool_calls": [{"tool": t["tool"], "arguments": t["arguments"]} for t in result["tool_calls"]],
            "pending_decisions": decisions, "seconds": result["seconds"]}


@router.post("/tenders/{tender_id}/decision")
def decide(tender_id: str, req: DecisionRequest) -> dict:
    try:
        return record_bid_decision(tender_id, req.decision, req.decided_by, req.note)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
