#!/usr/bin/env bash
# Smoke test: is the deployed app healthy end to end?
#
#   scripts/smoke.sh                       # test the live URL
#   scripts/smoke.sh http://localhost:8010 # test a local server
#
# Exits 0 only if every check passes, so CI/CD can block a bad release.
# Cost: one /ask call (~$0.0007).
set -uo pipefail

URL="${1:-https://apple-rag.agreeablestone-6fb7e6d2.australiaeast.azurecontainerapps.io}"
URL="${URL%/}"
FAILED=0

pass() { printf "  PASS  %s\n" "$1"; }
fail() { printf "  FAIL  %s\n" "$1"; FAILED=1; }

echo "Smoke test: $URL"

# 1. Health, with retries: a scaled-to-zero app needs a few seconds to wake up.
code=""
for _ in $(seq 1 12); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL/health")
  [ "$code" = "200" ] && break
  sleep 5
done
[ "$code" = "200" ] && pass "/health returns 200" || fail "/health returned $code"

# 2. Chat page is served.
if curl -s --max-time 15 "$URL/" | grep -q "<title>Apple Support Assistant"; then
  pass "/ serves the chat page"
else
  fail "/ did not serve the chat page"
fi

# 3. Documents list: proves the app can reach Azure AI Search with its identity.
docs=$(curl -s --max-time 15 "$URL/documents")
if echo "$docs" | grep -q '"chunks"'; then
  pass "/documents lists the index ($(echo "$docs" | grep -o '"source":"[^"]*"' | wc -l | tr -d ' ') documents)"
else
  fail "/documents returned: ${docs:0:120}"
fi

# 4. One real question: embed -> search -> generate, answer must cite a page.
answer=$(curl -s --max-time 60 -X POST "$URL/ask" -H 'Content-Type: application/json' \
  -d '{"question": "How do I take a screenshot?"}')
if echo "$answer" | grep -q '"answer"' && echo "$answer" | grep -qi 'page'; then
  pass "/ask answers with a page citation"
else
  fail "/ask returned: ${answer:0:160}"
fi

# 5. Security boundary: uploads require the API key.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST "$URL/upload" \
  -F 'file=@/dev/null;filename=smoke.pdf')
[ "$code" = "401" ] && pass "/upload without key returns 401" || fail "/upload without key returned $code"

if [ "$FAILED" -eq 0 ]; then echo "RESULT: PASS"; else echo "RESULT: FAIL"; fi
exit "$FAILED"
