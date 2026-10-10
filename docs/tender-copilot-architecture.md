# Tender Copilot: architecture (living document)

Three agents help a bid team respond to government tenders. **Agent 1** finds tenders that match the company
profile, **Agent 2** analyses a tender and drafts a grounded response, and **Agent 3** applies the company template
and design. Agents hand work over through **storage**, not by calling each other directly.

Status: **design v2** (three-agent pipeline). Iterated as we build; see the decision log.

---

## The pipeline

```
 AGENT 1 · Tender Scout        BLOB STORAGE (handoff)            AGENT 2 · Bid Analyst (Azure AI Foundry)         SHAREPOINT                AGENT 3 · Formatter
 finds tenders matching   ──►  tenders/{id}/notice.json  ──►  ingest → AI Search (tender-docs)              ──►  {SP_FOLDER}/{id}/     ──►  Power Automate (built):
 the profile (overview)        tenders/{id}/source/*.pdf      tools: score · extract · ask · draft               draft.docx                applies template + design,
                               tenders/{id}/status.json       ✋ human bid / no-bid gate before drafting                                   saves formatted .docx back
```

**ELI5:** three workers who pass folders through trays. If one is offline, the folder waits safely in the tray.

## Agent 1: Tender Scout
- **Job:** find new tender notices and keep the ones whose overview matches the company profile.
- **Sources, legitimately:** notices pasted in (MVP) → AusTender **alert emails** parsed by Power Automate (preferred
  later). No scraping, no shared logins.
- **Output:** `tenders/{id}/notice.json` (+ `source/*.pdf` when a registered user downloads the documents), `status = new`.

## Handoff storage (Blob)

```
container: tenders
tenders/{tender_id}/
├── notice.json        title, agency, closes, category, description
├── source/*.pdf       tender documents
├── status.json        new → scored → approved_to_bid | no_bid → drafted → sent_to_sharepoint → formatted
├── score.json         output of score_fit
├── requirements.json  output of extract_requirements (compliance matrix)
└── draft/
    ├── draft.json     structured draft {section_id, heading, text, sources} (audit + evals)
    └── draft.docx     handed to SharePoint for Agent 3
```

Why Blob: agents stay independent (different schedules), files survive restarts, full audit trail, and an event
(blob created) can trigger ingestion. Keyless: shared-key access disabled; identities use RBAC.

## Agent 2: Bid Analyst (Azure AI Foundry agent)

| # | Tool | Does | Output |
|---|---|---|---|
| 1 | `get_tender(tender_id)` | Read notice + status | tender summary |
| 2 | `score_fit(tender_id)` | Compare with the company profile | `{score, verdict, reasons, gaps}` (structured output) → `score.json` |
| 3 | `extract_requirements(tender_id)` | Pull the key tables: mandatory conditions, evaluation criteria + weights, response format + page limits, key dates | compliance matrix → `requirements.json` |
| 4 | `ask_tender(tender_id, question)` | Q&A over one tender (AI Search, `tender_id` filter) | `{answer, citations}` |
| 5 | `draft_response(tender_id)` | One section per evaluation criterion, grounded in tender + profile, within page limits | `draft.json` + `draft.docx` |
| 6 | `send_to_sharepoint(tender_id)` | Upload `draft.docx` to `{SP_FOLDER}/{id}/` via a Power Automate HTTP flow | SharePoint link |

**Human gate:** `score_fit` + `extract_requirements` → **bid manager decides bid / no-bid** → only then
`draft_response` and `send_to_sharepoint`. Drafting never starts without a human "bid".

**Ingestion:** MVP = a function call; later = Blob created → Event Grid → Azure Function → `di_ingest` →
AI Search `tender-docs` (each chunk tagged with `tender_id`).

## Agent 3: Formatter (Power Automate, already built)
- **Input:** `draft.docx` in `{SP_FOLDER}/{tender_id}/`.
- **Does:** applies the company template and document design, saves the formatted response back to SharePoint.
- `SP_FOLDER` is a setting (placeholder `/Tenders`); the site is decided later.

## Why it's split this way

| Decision | Reason |
|---|---|
| Handoff through storage, not agent-to-agent calls | Decoupled, resilient, auditable; each agent can be replaced or rescheduled independently |
| AI logic in Python tools | Unit-tested, evaluated, versioned, deployed by CI/CD |
| Formatting in Power Automate (Agent 3) | Reuses the existing, governed M365 flow and company templates |
| Human bid / no-bid gate | Real bid practice; nothing costly or external happens without a human decision |
| One index, `tender_id` filter | Simple, cheap; per-tender isolation by filter |
| Keyless storage + managed identity | No secrets to leak; least-privilege RBAC per identity |

## Build order (Agent 2)

1. Blob container `tenders` + handoff layout (T-001, T-009)
2. Ingest from Blob → AI Search `tender-docs`
3. Tools 1–4 as plain Python, tested on T-001 and T-009
4. Foundry agent wired to tools 1–4 + bid / no-bid gate
5. `draft_response` → `draft.docx`
6. `send_to_sharepoint` → Agent 3
7. Event Grid + Azure Function trigger for ingestion

## Decision log

| Date | Decision |
|---|---|
| 2026-10-08 | Scenario: Tender Copilot |
| 2026-10-08 | Profile: `tender/data/horsell_profile.md` (public-safe summary, local only, git-ignored); fictional tenders for testing |
| 2026-10-08 | AusTender blocks automated access (HTTP 403): no scraping or shared logins; alert emails later |
| 2026-10-09 | **v2: three agents** (Scout → Bid Analyst → Formatter), handoff through Blob and SharePoint |
| 2026-10-09 | Agent 3 (existing Power Automate flow) takes **.docx** input; SharePoint folder is a setting, placeholder `/Tenders/{tender_id}/`, site TBD |
| 2026-10-09 | Separate resource group `rg-tender-copilot` (clean tear-down, separate from the live Apple bot) |
