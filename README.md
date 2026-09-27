# Vera — magicpin AI Challenge bot (Aditya Ahlawat)

## Approach
**Grounded playbooks, not free-form generation.** Every message is built by a playbook for its trigger kind
(26 kinds + fallbacks) on top of a *fact layer* that can only print numbers, dates, names and sources that
exist in the pushed contexts. That kills the #1 score-killer in the rubric: fabrication.

Each playbook follows the same shape the case studies reward:
1. Owner's name first, no preamble, **why-now** in sentence one.
2. A verifiable anchor (source + page, batch numbers, their own CTR vs peer, days to deadline).
3. **Judgment, not templating**, whenever the data supports it, e.g.
   - Sunday IPL match: advise *against* a dine-in promo, save the Tue-Thu BOGO for a weeknight, and warn that 4 reviews already flag late delivery.
   - Diwali 188 days out: too early to promote; capture the April-May bridal window that's open now.
   - Competitor at ₹199: don't price-war; lead with the review theme patients already praise, fix the wait-time gap.
   - Chronic refill for atorvastatin: cross-checks the live batch recall and says only safe batches go out.
   - Renewal due with a weak month: fix-first, not hard-sell.
4. One low-friction CTA at the end; Hinglish where the merchant/customer speaks Hindi.

**Validator gate.** An optional LLM (Anthropic/OpenAI/Gemini/Groq/DeepSeek/OpenRouter via env) is used for trigger
kinds never seen before and for free-form questions. Its output only ships if it passes grounding (every number
must exist in context), taboo-word, URL, length and name checks; otherwise the playbook draft ships. So the bot's
floor is the deterministic output, and it stays up and fast even with no API key.

**Conversation engine.** Opt-out/hostility → `end` and 30-day mute. WA-Business auto-reply → one owner nudge,
then `wait` 24h, then `end` (tracked per merchant across conversations). Explicit commitment ("let's do it",
"haan kar do") → action mode immediately: deliver the artifact, never re-qualify. Off-topic (GST etc.) → one-line
decline + redirect. Price questions are answered only with numbers already in the thread. Per-turn Hinglish detection.

**Tick policy (restraint).** Skips expired triggers, used suppression keys, muted merchants, and customers without
consent. Sends at most one merchant-facing message per merchant per tick (highest urgency first) and never resends a
body a recipient already got.

## Tradeoffs
- Determinism over variety: the same inputs always give the same message (the brief asks for this), and it is
  instant (<10 ms per call). The cost is less stylistic range than a pure LLM.
- Generated triggers with `placeholder` payloads get composed from the merchant's own data (worst/best 7-day metric,
  review themes, peer gaps, category seasonal beats) rather than inventing specifics.
- Where data is inconsistent (e.g. Priya's last visit is after "now"), the message states the recall window and
  the last-visit date from the payload instead of computing a "months since" figure.

## What additional context would help most
Real appointment/slot availability per merchant, review counts on every merchant, the merchant's catalogue prices
for services they haven't made into offers, and a customer↔batch dispensing log (to say "22 of your customers are
affected" instead of "I'll filter the list").

## Run it
```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080 --workers 1     # ONE worker: state is in memory
python tools/harness.py http://localhost:8080               # full judge-lifecycle self-test
python tools/preview.py                                     # print all 30 test-pair messages
python make_submission.py                                   # writes submission.jsonl
```
Deploy: `render.yaml`, `Dockerfile` and `Procfile` included. Use an always-on instance (free tiers sleep and fail
`/v1/healthz`). Then run `judge_simulator.py` with your LLM key pointed at the public URL.

## Layout
`app.py` (5 endpoints + `/v1/teardown`) · `bot.py` (`compose()` per brief) · `conversation_handlers.py` (`respond()`) ·
`vera/facts.py` (grounded facts) · `vera/playbooks.py` (per-kind composers) · `vera/compose.py` (validator + LLM gate) ·
`vera/conversation.py` (reply engine) · `vera/llm.py` (provider-agnostic client) · `tools/` (harness, preview, dataset).
