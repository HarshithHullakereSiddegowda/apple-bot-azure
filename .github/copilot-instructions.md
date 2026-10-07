# Copilot instructions for this repository

Python 3.12 FastAPI RAG service on Azure (Azure OpenAI, Azure AI Search, Document Intelligence, Container Apps).
See `CLAUDE.md` for architecture and `ROADMAP.md` for planned work.

## Conventions
- All configuration and Azure clients come from `config.py`. Don't create clients or read env vars elsewhere.
- Auth: keys only for local development (`.env`); in Azure use managed identity via `DefaultAzureCredential`.
  Never hard-code endpoints, keys or connection strings.
- Model calls use deployment names (`chat`, `embed`), not model names. Use `temperature=0` for generation and judges.
- Logs are one JSON line per event (`event`, `request_id`, `latency_ms`, `tokens_in`, `tokens_out`, `caller`).
  Never log raw IPs, keys or full prompts.
- Every external call needs a timeout; batch jobs must retry HTTP 429 with backoff.
- New routes that write data or cost money require `require_api_key`; public routes require a rate limit.

## Before suggesting a change is done
- Prompt, model, chunking, retrieval or index changes: run `python evals/run_evals.py` and compare with the
  saved reports in `evals/`.
- Deployable changes: `scripts/smoke.sh <url>` must pass.
- Never commit `.env` or anything under `data/` (the source manual is copyrighted).
