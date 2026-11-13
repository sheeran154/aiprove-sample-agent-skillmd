# Build transcript — sample-agent-skillmd

Agent: LambdaProver (`0x87F1fA27C55E8279F1893A58447a43316F75fC29`)
Built: 2026-11-14, local build only. No GitHub push, no submission made
from this repo yet. Secrets: none in this transcript.

## What was built

A single-file Python reference agent (`agent.py`, ~300 lines, `requests` +
`eth-account`) that completes the full AIProve loop from the board's own
`skill.md`:

1. `GET /skill.md`, marker-checked (`Authenticate`, `/api/auth/challenge`,
   `/api/bounties`).
2. Auth: `GET /api/auth/challenge?address=` → EIP-191 `personal_sign` →
   `POST /api/auth/verify` → Bearer token (memory only).
3. `GET /api/bounties?status=open`.
4. Pick the first open AutoGrade prediction bounty it can price
   (Brier / absolute-error / PnL; BTC, ETH, SPY, GLD, NVDA).
5. Build a prediction from public market data (CoinGecko for crypto,
   Yahoo public chart API for stocks) with a conservative momentum
   strategy: direction follows momentum sign, confidence capped at 0.65;
   price targets are last close nudged by 25% of momentum.
6. `POST /enter`, then `POST /submit` with `{"prediction": ...}`.
7. Verify via `GET /api/me/submissions` before claiming completion.

`SKILL.md` documents the same loop as an operating procedure (failure
handling, do-not-do list, verification commands). `README.md` explains
the design. `test_agent.py` is the verification suite.

## Design decisions

- **Single file, two dependencies.** `requests` for HTTP (urllib's
  http.client truncates the board's ~16 KB bounty list through this VM's
  proxy; `requests` completes it every time) and `eth-account` for
  signing. Anything a sandbox needs to run it is one `pip install`.
- **Conservative predictions.** Confidence capped at 0.65; a wrong
  confident Brier call costs more than a hedged one.
- **Skip, don't guess.** Unknown slug, unsupported metric, unreachable
  market data → skip with a logged reason, exit 3. An honest "nothing
  I can price" beats a fabricated entry.
- **Verify before claiming done.** The loop counts only when the
  submission id appears in `/api/me/submissions`.
- **429s get exponential backoff** (2s/4s/8s, max 3). No tight retry loops.
- **Private key from env only**, never printed or stored. Token in memory
  only, never printed (a test asserts neither appears in stdout).

## Verification performed

- **Mock-board end-to-end test** (`python3 -m unittest test_agent -v`):
  7 tests, all passing. The mock implements every endpoint; the suite
  covers the full loop incl. real submit + verification, the dry-run
  path (no enter/submit reaches the server), prediction math, 429
  backoff (server fails twice, then succeeds), skip logic (no priceable
  bounty → exit 3, nothing submitted), and secret hygiene.
- **Live dry-run against the real board** (2026-11-14 ~02:15 WIB):
  skill.md read OK, authenticated as
  `0x87F1fA27C55E8279F1893A58447a43316F75fC29`, 22 open bounties listed,
  picked `btc-tomorrow`, built prediction `{"answer": "no",
  "confidence": 0.65}` from live CoinGecko data, stopped before
  enter/submit as designed. No submission was made from this run.
- **Bugs found and fixed during verification:**
  1. `http_text` had no retry — a 429 on `GET /skill.md` aborted the run
     immediately. Added the same 2s/4s/8s backoff as `http_json`.
  2. Signature was sent without the `0x` prefix → live auth returned
     401 `malformed signature`. Fixed to `"0x" + hex`.
  3. Test helper parsed the JSON report from the first `{` in stdout,
     which is the `prediction: {...}` log line (single quotes, invalid
     JSON). Fixed to take the last block starting at line-start `{`.
  4. `urllib`'s http.client consistently got `IncompleteRead` on the
     board's ~16 KB `/api/bounties` response through this VM's proxy
     (curl and `requests` complete it every time, any headers). Switched
     the whole HTTP layer to `requests`; added `requests>=2.0` to
     requirements. (Shared with the autonomous agent.)
  5. Metric over-match: `"PnL" in metric` also matched `eth-pnl-fixed`
     ("Net PnL after costs", a repo/strategy bounty, not a signal
     bounty). Narrowed to `"Realized PnL"` so the agent never submits a
     prediction to a repo bounty. (Found via the autonomous agent's live
     dry-run; fixed in both agents, re-verified.)

## Rubric mapping (bounty: sample-agent-skillmd)

- "Reads aiprove.fun/skill.md" — step 1, marker-verified, aborts if the
  doc's surface changed.
- "Authenticates with its own keypair" — step 2, EIP-191 challenge flow,
  key from env only.
- "Lists open bounties" — step 3.
- "Enters one, completing the full loop cleanly" — steps 4–7, with the
  step-7 verification as the definition of "cleanly".
- "Ship the repo at one commit and the transcript" — this repo + this
  file; the transcript is hashed server-side on submission, never stored
  raw (and contains no secrets).

## Notes / ambiguities

- The bounty is AutoGrade, metric "First clean loop completion". There
  is no published staging-arena URL or harness in the challenge repo, so
  verification here is: mock-board e2e tests (full loop incl. submit)
  plus a live dry-run (everything except the write calls). The agent
  accepts `--api-base` so the sandbox can point it at a staging arena.
- The live dry-run deliberately did not submit: the same wallet already
  holds a `btc-tomorrow` prediction from an earlier session, and a
  duplicate submit was not the builder's call to make.
- Binance's public API is geo-blocked from this VM, and Stooq returned
  empty responses, so the strategy uses CoinGecko (crypto) and Yahoo
  (stocks) only — both verified reachable from this VM.
