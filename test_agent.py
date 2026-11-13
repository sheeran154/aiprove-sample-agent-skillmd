"""Tests for the sample AIProve agent.

Runs the full loop against a local mock board (no network, no real key):
- end-to-end loop incl. a real submit + verification step
- dry-run path (no enter/submit calls reach the server)
- prediction math (momentum -> answer / confidence / price target)
- 429 backoff (server fails twice, then succeeds)
- skip logic (no priceable bounty -> exit 3, nothing submitted)
- secret hygiene (private key and token never appear in stdout)

Run:  python3 -m pytest test_agent.py -v
       (or: python3 -m unittest test_agent -v)
"""

import io
import json
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402

TEST_KEY = "0x" + "11" * 32  # throwaway key, test-only, never a real wallet


def make_mock_server(fail_429_times=0):
    """A mock AIProve board. Returns (server, state)."""
    state = {
        "calls": [],
        "submissions": [],
        "fail_429_remaining": fail_429_times,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            pass

        def _send(self, status, obj):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _maybe_429(self):
            if state["fail_429_remaining"] > 0:
                state["fail_429_remaining"] -= 1
                self._send(429, {"error": "slow down"})
                return True
            return False

        def do_GET(self):
            state["calls"].append(("GET", self.path))
            if self._maybe_429():
                return
            if self.path == "/skill.md":
                body = ("# skill.md\nAuthenticate\n/api/auth/challenge\n"
                        "/api/bounties\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api/auth/challenge"):
                self._send(200, {"challenge": "mock-challenge-123"})
            elif self.path == "/api/bounties?status=open":
                self._send(200, {"bounties": [
                    {"slug": "btc-tomorrow", "title": "Will BTC close higher tomorrow?",
                     "lane": "AutoGrade", "metric": "Brier score", "status": "open"},
                    {"slug": "make-meme-aiprove", "title": "Make a meme",
                     "lane": "PosterJudge", "metric": "Poster rubric", "status": "open"},
                ]})
            elif self.path == "/api/me/submissions":
                self._send(200, {"submissions": state["submissions"]})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                payload = {}
            state["calls"].append(("POST", self.path, payload))
            if self._maybe_429():
                return
            if self.path == "/api/auth/verify":
                assert payload.get("address"), "verify needs an address"
                assert payload.get("signature"), "verify needs a signature"
                self._send(200, {"token": "mock-token-abc", "expiresAt": 9999999999999})
            elif self.path.endswith("/enter"):
                self._send(200, {"ok": True})
            elif self.path.endswith("/submit"):
                sub = {"id": "sub-1", "bountySlug": self.path.split("/")[3],
                       "prediction": payload.get("prediction"),
                       "status": "provenance_ok"}
                state["submissions"].append(sub)
                self._send(201, sub)
            else:
                self._send(404, {"error": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["AGENT_PRIVATE_KEY"] = TEST_KEY
        # Deterministic market data: BTC fell ~5% over the window.
        self._orig_btc = agent.coingecko_closes
        self._orig_yahoo = agent.yahoo_closes
        agent.coingecko_closes = lambda coin_id, days=7: [75000, 74000, 73000, 72000, 71000]
        agent.yahoo_closes = lambda symbol, range_="5d": [750.0, 752.0, 751.0]

    def tearDown(self):
        agent.coingecko_closes = self._orig_btc
        agent.yahoo_closes = self._orig_yahoo

    def run_agent(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = agent.main(list(argv))
        return code, out.getvalue()

    def report_of(self, out):
        # The JSON report is the last block starting with "{" at line start
        # (earlier log lines also contain dicts, e.g. the prediction line).
        return json.loads(out[out.rindex("\n{") + 1:])

    def test_full_loop_end_to_end(self):
        server, state = make_mock_server()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, out = self.run_agent("--api-base", base, "--bounty", "btc-tomorrow")
            self.assertEqual(code, 0, out)
            report = self.report_of(out)
            self.assertEqual(report["steps"]["verify"], "ok")
            self.assertEqual(report["submission_id"], "sub-1")
            # Momentum is negative -> "no" with a hedged confidence.
            self.assertEqual(report["prediction"]["answer"], "no")
            self.assertLessEqual(report["prediction"]["confidence"], 0.65)
            posts = [c for c in state["calls"] if c[0] == "POST"]
            self.assertTrue(any("/enter" in c[1] for c in posts))
            self.assertTrue(any("/submit" in c[1] for c in posts))
        finally:
            server.shutdown()

    def test_dry_run_submits_nothing(self):
        server, state = make_mock_server()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, out = self.run_agent("--api-base", base, "--dry-run")
            self.assertEqual(code, 0, out)
            posts = [c for c in state["calls"] if c[0] == "POST"]
            # Only the auth verify POST may happen; no enter/submit.
            self.assertFalse(any("/enter" in c[1] or "/submit" in c[1] for c in posts))
            self.assertEqual(state["submissions"], [])
        finally:
            server.shutdown()

    def test_prediction_math(self):
        # Strong downtrend -> "no", confidence capped at 0.65.
        pred, reason = agent.build_prediction("btc-tomorrow", "Brier score")
        self.assertIsNone(reason)
        self.assertEqual(pred["answer"], "no")
        # momentum -5.33% -> confidence 0.55 + min(0.08, 0.10) = 0.63
        self.assertEqual(pred["confidence"], 0.63)
        # Price target: last close nudged by a quarter of momentum.
        pred, _ = agent.build_prediction("spy-close-1dollar", "Absolute error, $")
        self.assertAlmostEqual(pred["answer"], 751.0 * (1 + (751.0 / 750.0 - 1) * 0.25), places=2)
        # Signal follows momentum sign.
        pred, _ = agent.build_prediction("signal-eth-pnl-24h", "Realized PnL on your call")
        self.assertEqual(pred["answer"], "short")

    def test_skip_unknown_bounty(self):
        pred, reason = agent.build_prediction("top5-pons-24h", "Overlap with realized top 5")
        self.assertIsNone(pred)
        self.assertIn("unsupported metric", reason)
        pred, reason = agent.build_prediction("refer-most-agents", "Verified referrals")
        self.assertIsNone(pred)

    def test_429_backoff_then_success(self):
        server, state = make_mock_server(fail_429_times=2)
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, out = self.run_agent("--api-base", base, "--bounty", "btc-tomorrow")
            self.assertEqual(code, 0, out)
        finally:
            server.shutdown()

    def test_no_priceable_bounty_exits_3(self):
        server, state = make_mock_server()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            # Point the agent at a board whose only open bounty is PosterJudge.
            orig = agent.list_open_bounties
            agent.list_open_bounties = lambda api_base: [
                {"slug": "make-meme-aiprove", "lane": "PosterJudge",
                 "metric": "Poster rubric", "status": "open"}]
            try:
                code, out = self.run_agent("--api-base", base)
            finally:
                agent.list_open_bounties = orig
            self.assertEqual(code, 3, out)
            self.assertEqual(state["submissions"], [])
        finally:
            server.shutdown()

    def test_secrets_never_in_output(self):
        server, state = make_mock_server()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, out = self.run_agent("--api-base", base, "--bounty", "btc-tomorrow")
            self.assertNotIn(TEST_KEY, out)
            self.assertNotIn("mock-token-abc", out)
            self.assertNotIn("11" * 32, out)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
