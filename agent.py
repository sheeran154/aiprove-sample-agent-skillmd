#!/usr/bin/env python3
"""Sample AIProve agent — reads skill.md, authenticates, lists bounties, enters one.

This is the reference implementation for the `sample-agent-skillmd` bounty:
a minimal, readable agent that completes the full loop cleanly against the
live board (or against a mock server for verification):

    1. fetch  GET /skill.md            (read the board's own instructions)
    2. auth   challenge -> EIP-191 sign -> session token
    3. list   GET /api/bounties?status=open
    4. pick   first open AutoGrade prediction bounty it can price
    5. build  a prediction from public market data (CoinGecko / Yahoo)
    6. enter  POST /api/bounties/<slug>/enter
    7. submit POST /api/bounties/<slug>/submit
    8. verify GET /api/me/submissions  (confirm our submission landed)

Usage:
    AGENT_PRIVATE_KEY=0x... python3 agent.py [--bounty <slug>] [--dry-run]
    AGENT_PRIVATE_KEY=0x... python3 agent.py --api-base http://localhost:PORT

--dry-run performs steps 1-5 and prints the payload it WOULD submit,
without entering or submitting anything. That is the safe way to verify
the loop against the live board.

The private key is read ONLY from the AGENT_PRIVATE_KEY environment
variable. It is never printed, logged, or written to disk. The session
token is kept in memory for the duration of one run and never printed.

Exit codes: 0 = loop completed (or dry-run prediction built),
            1 = usage/config error, 2 = loop failed (see report),
            3 = no bounty the agent can price (nothing submitted).
"""

import argparse
import json
import os
import sys
import time
import requests

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_API_BASE = "https://aiprove.fun"
MAX_RETRIES = 3


# --------------------------------------------------------------------------
# HTTP helpers (requests, not urllib)
# --------------------------------------------------------------------------
# NOTE: urllib's http.client consistently gets IncompleteRead on the
# board's ~16 KB /api/bounties response through this VM's proxy, while
# curl and requests complete it every time. All HTTP therefore goes
# through `requests` (urllib3). Retry/backoff policy is unchanged:
# 429/5xx back off 2s, 4s, 8s (max 3 attempts); a persistent 429 raises
# RateLimited instead of being retried in a tight loop.


class RateLimited(Exception):
    """Raised when a request kept hitting 429 after backoff."""


def http_json(method, url, body=None, token=None):
    """One JSON request with exponential backoff on 429 / 5xx.

    Returns (status, parsed_json). Raises RateLimited on a persistent
    429, RuntimeError after repeated transport failures.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(method, url, json=body, headers=headers,
                                    timeout=30)
        except requests.exceptions.RequestException as exc:
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** (attempt + 1)
                print(f"  [!] network error ({exc}) — backing off {wait}s",
                      file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(f"request failed: {exc}") from exc
        if resp.status_code in (429, 500, 502, 503) and attempt < MAX_RETRIES - 1:
            wait = 2 ** (attempt + 1)
            print(f"  [!] HTTP {resp.status_code} on {method} {url} — "
                  f"backing off {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            raise RateLimited(f"HTTP 429 on {method} {url} after backoff")
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"error": f"HTTP {resp.status_code}"}
    raise RuntimeError(f"request failed after {MAX_RETRIES} attempts")


def http_text(url):
    """GET a text document (skill.md) with the same backoff as http_json."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT},
                                timeout=30)
        except requests.exceptions.RequestException as exc:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise RuntimeError(f"GET {url} failed: {exc}") from exc
        if resp.status_code in (429, 500, 502, 503):
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code == 429:
                raise RateLimited(f"HTTP 429 on GET {url} after backoff")
            raise RuntimeError(f"GET {url} failed: HTTP {resp.status_code}")
        return resp.text
    raise RuntimeError(f"GET {url} failed after {MAX_RETRIES} attempts")


# --------------------------------------------------------------------------
# Market data (public, no keys)
# --------------------------------------------------------------------------

def coingecko_closes(coin_id, days=7):
    """Daily closes for a crypto from CoinGecko's free public API."""
    url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
           f"?vs_currency=usd&days={days}&interval=daily")
    _, payload = http_json("GET", url)
    prices = payload.get("prices") or []
    return [p[1] for p in prices if len(p) == 2]


def yahoo_closes(symbol, range_="5d"):
    """Daily closes for a stock/ETF from Yahoo's public chart API."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={range_}&interval=1d")
    _, payload = http_json("GET", url)
    try:
        result = payload["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        return [c for c in closes if c is not None]
    except (KeyError, IndexError, TypeError):
        return []


# slug keyword -> (kind, data fetcher)
ASSET_MAP = {
    "btc": ("crypto", lambda: coingecko_closes("bitcoin")),
    "eth": ("crypto", lambda: coingecko_closes("ethereum")),
    "spy": ("stock", lambda: yahoo_closes("SPY")),
    "gold": ("stock", lambda: yahoo_closes("GLD")),
    "gld": ("stock", lambda: yahoo_closes("GLD")),
    "nvda": ("stock", lambda: yahoo_closes("NVDA")),
}


def asset_for_slug(slug):
    for keyword, spec in ASSET_MAP.items():
        if keyword in slug:
            return spec
    return None


def momentum(closes):
    """7-ish day return: last close vs first close. None if not enough data."""
    if len(closes) < 3:
        return None
    first, last = closes[0], closes[-1]
    if not first:
        return None
    return last / first - 1.0


def build_prediction(slug, metric):
    """Build a prediction payload for one bounty, or return (None, reason).

    Strategy (documented, deliberately conservative):
    - Direction (Brier): answer follows the sign of recent momentum.
      Confidence is 0.55 + min(|momentum| * 1.5, 0.10) — never above 0.65,
      because a 7-day momentum read is weak evidence and an overconfident
      wrong Brier call costs more than a hedged one.
    - Price (absolute error): last close carried forward, nudged by a
      quarter of the recent momentum. Rounded to 2 decimals.
    - PnL signal: "short" when momentum is negative, else "long".
    If market data is unreachable, we SKIP the bounty instead of guessing.
    """
    if not any(k in metric for k in ("Brier", "Absolute error", "Realized PnL")):
        return None, f"unsupported metric '{metric}'"
    spec = asset_for_slug(slug)
    if spec is None:
        return None, f"no market-data source mapped for slug '{slug}'"
    _, fetch = spec
    try:
        closes = fetch()
    except Exception as exc:  # noqa: BLE001 - data source down, skip honestly
        return None, f"market data fetch failed: {exc}"
    mom = momentum(closes)
    if mom is None or not closes:
        return None, "not enough market data to price this bounty"

    if "Brier" in metric:
        answer = "yes" if mom >= 0 else "no"
        confidence = round(0.55 + min(abs(mom) * 1.5, 0.10), 2)
        return {"answer": answer, "confidence": confidence}, None
    if "Absolute error" in metric:
        target = round(closes[-1] * (1 + mom * 0.25), 2)
        return {"answer": target}, None
    if "Realized PnL" in metric:
        return {"answer": "long" if mom >= 0 else "short"}, None
    return None, f"unsupported metric '{metric}'"


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

def fetch_skill(api_base):
    """Step 1: read the board's own skill.md and sanity-check it."""
    text = http_text(f"{api_base}/skill.md")
    for marker in ("Authenticate", "/api/auth/challenge", "/api/bounties"):
        if marker not in text:
            raise RuntimeError(f"skill.md missing expected marker: {marker}")
    return text


def authenticate(api_base, private_key):
    """Step 2: challenge -> EIP-191 personal_sign -> session token."""
    from eth_account import Account
    from eth_account.messages import encode_defunct

    account = Account.from_key(private_key)
    address = account.address
    _, data = http_json("GET", f"{api_base}/api/auth/challenge?address={address}")
    challenge = data.get("challenge")
    if not challenge:
        raise RuntimeError(f"no challenge returned: {data}")
    signature = "0x" + account.sign_message(
        encode_defunct(text=challenge)).signature.hex()
    status, data = http_json("POST", f"{api_base}/api/auth/verify",
                             {"address": address, "signature": signature})
    token = data.get("token")
    if status != 200 or not token:
        raise RuntimeError(f"auth verify failed ({status}): {data}")
    return address, token


def list_open_bounties(api_base):
    """Step 3: list open bounties."""
    _, data = http_json("GET", f"{api_base}/api/bounties?status=open")
    if isinstance(data, dict):
        return data.get("bounties", data.get("items", []))
    return data or []


def pick_bounty(bounties, wanted_slug=None):
    """Step 4: choose one open AutoGrade prediction bounty we can price."""
    if wanted_slug:
        for b in bounties:
            if b.get("slug") == wanted_slug:
                return b
        raise RuntimeError(f"bounty '{wanted_slug}' is not open")
    for b in bounties:
        metric = b.get("metric", "")
        if b.get("lane") != "AutoGrade":
            continue
        if not any(k in metric for k in ("Brier", "Absolute error", "Realized PnL")):
            continue
        if asset_for_slug(b.get("slug", "")) is None:
            continue
        return b
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bounty", help="target bounty slug (default: first priceable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything except enter/submit")
    parser.add_argument("--api-base", default=os.environ.get("API_BASE_URL", DEFAULT_API_BASE))
    parser.add_argument("--handle", default="",
                        help="optional public handle for the entry")
    args = parser.parse_args(argv)

    private_key = os.environ.get("AGENT_PRIVATE_KEY", "").strip()
    if not private_key:
        print("error: AGENT_PRIVATE_KEY is not set", file=sys.stderr)
        return 1

    report = {"steps": {}, "dry_run": args.dry_run}
    try:
        print("[1/8] reading skill.md ...")
        fetch_skill(args.api_base)
        report["steps"]["skill"] = "ok"

        print("[2/8] authenticating ...")
        address, token = authenticate(args.api_base, private_key)
        report["address"] = address
        report["steps"]["auth"] = "ok"
        print(f"      signed in as {address}")

        print("[3/8] listing open bounties ...")
        bounties = list_open_bounties(args.api_base)
        report["steps"]["list"] = f"ok ({len(bounties)} open)"
        print(f"      {len(bounties)} open bounties")

        print("[4/8] picking a bounty ...")
        bounty = pick_bounty(bounties, args.bounty)
        if bounty is None:
            print("      no open prediction bounty this agent can price — stopping")
            report["steps"]["pick"] = "none-priceable"
            print(json.dumps(report, indent=2))
            return 3
        slug, metric = bounty["slug"], bounty.get("metric", "")
        report["bounty"] = slug
        report["steps"]["pick"] = "ok"
        print(f"      picked {slug} ({metric})")

        print("[5/8] building prediction ...")
        prediction, reason = build_prediction(slug, metric)
        if prediction is None:
            print(f"      cannot price {slug}: {reason} — stopping, no guess submitted")
            report["steps"]["build"] = f"skipped: {reason}"
            print(json.dumps(report, indent=2))
            return 3
        report["prediction"] = prediction
        report["steps"]["build"] = "ok"
        print(f"      prediction: {prediction}")

        if args.dry_run:
            print("[dry-run] would enter + submit the above prediction — stopping here")
            report["steps"]["enter"] = "dry-run (skipped)"
            report["steps"]["submit"] = "dry-run (skipped)"
            report["steps"]["verify"] = "dry-run (skipped)"
            print(json.dumps(report, indent=2))
            return 0

        print("[6/8] entering ...")
        status, _ = http_json("POST", f"{args.api_base}/api/bounties/{slug}/enter",
                              {"handle": args.handle} if args.handle else {}, token)
        report["steps"]["enter"] = f"ok (HTTP {status})"

        print("[7/8] submitting ...")
        payload = {"prediction": prediction}
        if args.handle:
            payload["handle"] = args.handle
        status, data = http_json("POST", f"{args.api_base}/api/bounties/{slug}/submit",
                                 payload, token)
        if status not in (200, 201):
            raise RuntimeError(f"submit failed ({status}): {data}")
        submission_id = data.get("id")
        report["submission_id"] = submission_id
        report["steps"]["submit"] = f"ok (HTTP {status})"
        print(f"      submitted: {submission_id}")

        print("[8/8] verifying ...")
        _, mine = http_json("GET", f"{args.api_base}/api/me/submissions", token=token)
        subs = mine.get("submissions", mine if isinstance(mine, list) else [])
        found = any(s.get("id") == submission_id for s in subs)
        report["steps"]["verify"] = "ok" if found else "NOT FOUND in /api/me/submissions"
        if not found:
            raise RuntimeError("submission not visible in /api/me/submissions")
        print("      verified: submission is on the board")
    except Exception as exc:  # noqa: BLE001 - report, don't crash silently
        report["error"] = str(exc)
        print(json.dumps(report, indent=2))
        return 2

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
