"""Agent 2 (Bid Analyst) on Azure AI Foundry Agent Service.

    BRICK 3  TOOLS      job cards (JSON schema) the agent reads to choose a tool
    BRICK 4  AGENT      model + instructions + job cards, created once in the Foundry project
    BRICK 5  run_turn   the loop: Foundry asks for a tool → OUR code runs it → result goes back → until done

Foundry never executes our Python. It pauses the run with REQUIRES_ACTION and names the tool and arguments;
this module runs the matching function from tender/tools.py and submits the output.

    python -m tender.agent "Should Horsell bid on T-009?"     # one question
    python -m tender.agent                                    # interactive chat (same thread)
"""
import json
import os
import sys
import time

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import FunctionDefinition, FunctionToolDefinition, ListSortOrder, RunStatus, ToolOutput
from azure.identity import DefaultAzureCredential

from tender import tools
from tender.draft import draft_response, send_to_formatter
from tender.ingest import ingest_tender

AGENT_NAME = "tender-bid-analyst"
AGENT_VERSION = "agent-v5"   # bump when instructions or tools change
MAX_TOOL_OUTPUT_CHARS = 8000   # keep tool results small enough for the model's context
MAX_TOOL_CALLS_PER_TURN = 12   # stop runaway loops

# ── BRICK 3: tool definitions (the job cards) ────────────────────────────────

_TID = {"type": "string", "description": "Tender ID, e.g. T-009 or ATM-105519"}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> FunctionToolDefinition:
    return FunctionToolDefinition(function=FunctionDefinition(
        name=name, description=description,
        parameters={"type": "object", "properties": properties, "required": required}))


TOOLS = [
    _tool("list_tenders", "List tenders in the tray with status, verdict and score. Use to find tender IDs. "
                          "STATUS is where a tender is in the process; VERDICT is the AI's fit assessment. "
                          "Tenders waiting for a human bid decision are status='awaiting_decision' (not a verdict).",
          {"status": {"type": "string",
                      "enum": ["new", "scored", "awaiting_decision", "approved_to_bid", "no_bid", "drafted",
                               "sent_to_formatter", "formatted"],
                      "description": "Optional process-status filter"}}, []),
    _tool("get_tender", "Notice details, status, documents and which analysis has run for one tender.",
          {"tender_id": _TID}, ["tender_id"]),
    _tool("score_fit", "Score one tender against Horsell's capability profile: verdict, score, service line, "
                       "reasons, gaps and mandatory blockers. Use before recommending bid or no-bid.",
          {"tender_id": _TID}, ["tender_id"]),
    _tool("ingest_tender", "Read and index a tender's source documents so they can be queried. Only needed when "
                           "get_tender shows documents but ingested is false.",
          {"tender_id": _TID}, ["tender_id"]),
    _tool("extract_requirements", "Full compliance checklist from the tender documents: mandatory conditions, "
                                  "evaluation criteria and weightings, response parts and page limits, key dates, "
                                  "pricing roles. Requires ingested documents.",
          {"tender_id": _TID}, ["tender_id"]),
    _tool("request_bid_decision", "Save your bid / no-bid RECOMMENDATION with reasoning and hand the decision to "
                                  "the bid manager. Call after score_fit when asked whether to bid. This does NOT "
                                  "decide; only a person can record the decision.",
          {"tender_id": _TID, "recommendation": {"type": "string", "enum": ["bid", "no_bid"]},
           "reasoning": {"type": "string", "description": "2-4 sentences: fit, blockers, what must be confirmed"}},
          ["tender_id", "recommendation", "reasoning"]),
    _tool("draft_response", "Write the first-draft bid response (.docx): compliance table plus one section per "
                            "evaluation criterion within page limits, with placeholders where evidence is missing. "
                            "Only works after a person has approved the bid; otherwise it returns an error.",
          {"tender_id": _TID}, ["tender_id"]),
    _tool("send_to_formatter", "Hand the finished draft (.docx) to the formatter (Agent 3), which applies the company "
                               "template. Use after draft_response when the user asks to send or finalise the draft.",
          {"tender_id": _TID}, ["tender_id"]),
    _tool("ask_tender", "Answer a specific question about one tender from its documents and notice, with page "
                        "citations. Use for anything not covered by the other tools.",
          {"tender_id": _TID, "question": {"type": "string"}}, ["tender_id", "question"]),
]

# Name on the job card → the Python function that does the work.
DISPATCH = {
    "list_tenders": tools.list_tenders,
    "get_tender": tools.get_tender,
    "score_fit": tools.score_fit,
    "ingest_tender": ingest_tender,
    "extract_requirements": tools.extract_requirements,
    "ask_tender": tools.ask_tender,
    "request_bid_decision": tools.request_bid_decision,
    "draft_response": draft_response,
    "send_to_formatter": send_to_formatter,
    # record_bid_decision is deliberately NOT here: recording the decision is a human-only action.
}

# ── BRICK 4: the agent (instructions + tools) ────────────────────────────────

INSTRUCTIONS = """You are Horsell's Bid Analyst. You help the bid manager decide whether to bid for government
tenders and understand what a bid requires.

How to work:
1. Find the tender (list_tenders or get_tender). Never guess a tender ID.
2. For "should we bid" questions, call score_fit and report: verdict, score, service line, main reasons, and
   every mandatory blocker exactly as the tool states it. Then call request_bid_decision with your
   recommendation and tell the user the decision is now waiting for them.
3. For questions about requirements, use extract_requirements (full checklist) or ask_tender (one question).
   If documents exist but are not ingested, call ingest_tender first.
4. Use only facts returned by tools. Keep page citations from tool results. If a tool says something is not
   stated, say so.

Rules:
- You RECOMMEND; the bid manager DECIDES. You cannot approve or reject a bid, even if asked to. If asked, explain
  that the bid manager records the decision in the app.
- After drafting, tell the user how many placeholders and warnings need human attention; the draft is not final.
- Report blockers faithfully: "not stated in the profile" is not the same as "not held".
- Be concise: short paragraphs or bullet points, numbers and dates exact."""


def client() -> AgentsClient:
    return AgentsClient(os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=DefaultAzureCredential())


def ensure_agent(c: AgentsClient):
    """Create the agent once; update it in place if it already exists (so instruction changes apply)."""
    model = os.environ.get("FOUNDRY_AGENT_MODEL", "agent-brain")
    existing = next((a for a in c.list_agents() if a.name == AGENT_NAME), None)
    if existing:
        return c.update_agent(existing.id, model=model, instructions=INSTRUCTIONS, tools=TOOLS)
    return c.create_agent(model=model, name=AGENT_NAME, instructions=INSTRUCTIONS, tools=TOOLS)


# ── BRICK 5: the run loop (where Foundry and our code meet) ──────────────────

def _run_tool(name: str, arguments: str) -> tuple[str, float]:
    """Execute one tool call with OUR code. Errors go back to the agent as data so it can recover."""
    t0 = time.perf_counter()
    try:
        fn = DISPATCH[name]
        result = fn(**json.loads(arguments or "{}"))
    except Exception as e:  # noqa: BLE001 - any failure is reported to the agent, never crashes the loop
        result = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    text = json.dumps(result, default=str)
    if len(text) > MAX_TOOL_OUTPUT_CHARS:
        text = text[:MAX_TOOL_OUTPUT_CHARS] + '..."(truncated)"'
    return text, time.perf_counter() - t0


def run_turn(c: AgentsClient, agent_id: str, thread_id: str, question: str, trace: bool = True) -> dict:
    """One user turn: send the question, service tool calls until the run completes, return answer + trace."""
    c.messages.create(thread_id=thread_id, role="user", content=question)
    run = c.runs.create(thread_id=thread_id, agent_id=agent_id)
    calls, t0 = [], time.perf_counter()
    while run.status in (RunStatus.QUEUED, RunStatus.IN_PROGRESS, RunStatus.REQUIRES_ACTION):
        if run.status == RunStatus.REQUIRES_ACTION:
            outputs = []
            for tc in run.required_action.submit_tool_outputs.tool_calls:
                if len(calls) >= MAX_TOOL_CALLS_PER_TURN:
                    output, secs = json.dumps({"error": "tool-call limit reached for this turn"}), 0.0
                else:
                    output, secs = _run_tool(tc.function.name, tc.function.arguments)
                calls.append({"tool": tc.function.name, "arguments": tc.function.arguments, "seconds": round(secs, 1),
                              "output": output[:3000]})   # kept (shortened) for evals and audit
                if trace:
                    print(f"   🔧 {tc.function.name}({tc.function.arguments})  → {len(output):,} chars, {secs:.1f}s")
                outputs.append(ToolOutput(tool_call_id=tc.id, output=output))
            run = c.runs.submit_tool_outputs(thread_id=thread_id, run_id=run.id, tool_outputs=outputs)
        else:
            time.sleep(0.8)
            run = c.runs.get(thread_id=thread_id, run_id=run.id)

    answer = ""
    if run.status == RunStatus.COMPLETED:
        last = c.messages.get_last_message_text_by_role(thread_id=thread_id, role="assistant")
        answer = last.text.value if last else ""
    usage = getattr(run, "usage", None)
    return {"status": str(run.status), "answer": answer, "tool_calls": calls,
            "seconds": round(time.perf_counter() - t0, 1),
            "tokens": getattr(usage, "total_tokens", None),
            "error": getattr(run, "last_error", None)}


def main() -> None:
    c = client()
    agent = ensure_agent(c)
    thread = c.threads.create()
    print(f"agent {agent.name} ({agent.id}) · thread {thread.id}")
    questions = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else None
    while True:
        q = questions.pop(0) if questions else (input("\nyou> ").strip() if questions is None else "")
        if not q:
            break
        r = run_turn(c, agent.id, thread.id, q)
        print(f"\nagent> {r['answer'] or r['error']}\n   [{r['status']} · {len(r['tool_calls'])} tool calls · "
              f"{r['seconds']}s · {r['tokens']} tokens]")
        if questions is not None and not questions:
            break
    _ = ListSortOrder  # kept for message listing in later bricks


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    main()
