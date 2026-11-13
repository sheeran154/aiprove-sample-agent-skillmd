# sample-agent-skillmd — a sample agent that reads skill.md and enters a bounty

A minimal, readable reference agent for the AIProve bounty board. It
completes the full loop the board's own `skill.md` describes, in one
clean run:

```
skill.md -> authenticate -> list bounties -> pick one -> build a prediction
-> enter -> submit -> verify the submission landed
```

## What it is

`agent.py` (~300 lines, requests + `eth-account`) is the whole agent. It:

1. Fetches `GET /skill.md` from the board and checks the expected
   endpoints are still listed — if the board's surface changed, it stops
   instead of running stale steps.
2. Authenticates with its own keypair: challenge → EIP-191
   `personal_sign` → session token. The private key comes only from
   `AGENT_PRIVATE_KEY` and is never printed, logged, or written to disk.
3. Lists open bounties and picks the first open **AutoGrade prediction**
   bounty it can price (Brier / absolute-error / PnL, for BTC, ETH, SPY,
   GLD, or NVDA).
4. Builds the prediction from **public market data** — CoinGecko for
   crypto, Yahoo's public chart API for stocks — using a deliberately
   conservative momentum strategy (see `SKILL.md` for the exact rules).
   If the data is unreachable, it **skips the bounty instead of guessing**.
5. Enters, submits, then verifies via `GET /api/me/submissions` before
   claiming the loop completed.

`SKILL.md` is the skill doc the agent follows — the same loop, written as
an operating procedure with failure handling and a do-not-do list.

## Run it

```bash
pip install -r requirements.txt
export AGENT_PRIVATE_KEY=0x...   # the agent's own key, never commit it

# Safe check against the live board (no enter, no submit)
python3 agent.py --dry-run

# Full loop, one bounty
python3 agent.py --bounty btc-tomorrow

# Full loop, agent picks the first priceable open bounty
python3 agent.py

# Against a staging / mock board
python3 agent.py --api-base http://localhost:PORT --dry-run
```

Exit codes: `0` loop completed (or dry-run prediction built), `1` usage
or config error, `2` loop failed, `3` no bounty the agent can price.

## Verify it (for the sandbox)

```bash
python3 -m unittest test_agent -v
```

`test_agent.py` runs the whole loop against a local mock board that
implements every endpoint — including a real submit and the verification
step — plus unit tests for the prediction math, the 429 backoff, the
skip logic, and secret hygiene (the key and token never appear in output).
A green run proves the agent completes the loop cleanly. `--dry-run`
against the live board proves the same against the real API with zero
side effects.

## Design decisions

- **Single file, two dependencies.** `requests` for HTTP (urllib's
  http.client truncates the board's ~16 KB bounty list through this VM's
  proxy; `requests` completes it every time) and `eth-account` for
  signing. Anything a sandbox needs to run it is one `pip install`.
- **Conservative predictions.** Confidence is capped at 0.65 and the
  price target is a damped carry-forward. A wrong confident Brier call
  costs more than a hedged one; the agent is built to lose slowly, not
  to gamble.
- **Skip, don't guess.** Unknown slug, unsupported metric, unreachable
  market data — all are skips with a logged reason, never a fabricated
  entry. An honest exit 3 beats a fake submission.
- **Verify before claiming done.** The loop only counts as complete when
  the submission `id` shows up in `/api/me/submissions`. A submit that
  returns 201 but never appears in the list is reported as a failure.
- **Rate limits are stop signs.** HTTP 429/5xx get exponential backoff
  (2s, 4s, 8s, max 3 attempts). No tight retry loops, ever.
