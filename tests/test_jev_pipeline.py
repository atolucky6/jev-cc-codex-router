"""Isolated pipeline checks against a controlled local optimizer."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from test_jev import CONFIG, COMPLEX


class Optimizer(BaseHTTPRequestHandler):
    requests = []
    response = {"choices": [{"message": {"content": "Clarified database request."}}]}

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        body = json.dumps(self.response).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(__file__).resolve().parents[1]
        shutil.copy2(root / "jev_router.py", cls.temp.name)
        spec = importlib.util.spec_from_file_location("isolated_jev_router", Path(cls.temp.name) / "jev_router.py")
        cls.router = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test"}, clear=True):
            spec.loader.exec_module(cls.router)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Optimizer)
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    def setUp(self):
        self.r = self.router
        Optimizer.requests = []
        Optimizer.response = {"choices": [{"message": {"content": "Clarified database request."}}]}
        for name, value in {
            "PRE_OPT_CONFIG": copy.deepcopy(CONFIG), "OPT_ENABLED": True,
            "OPT_MODEL": "legacy-model", "OPT_URL": "http://127.0.0.1:%d" % self.server.server_port,
            "REWRITE": True, "PLANNING_BOOST": False,
        }.items():
            p = patch.object(self.r, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        self.r.LEASE.clear()
        self.r.LEASE_EFFORT.clear()
        self.r.BASE.clear()
        p = patch.object(self.r, "ask_jev", return_value=("terra", .9, "medium", .9))
        self.ask = p.start()
        self.addCleanup(p.stop)

    def rewrite(self, prompt=COMPLEX, metadata=None):
        body = {"model": "incoming", "input": prompt}
        if metadata is not None:
            body["metadata"] = metadata
        raw, meta = self.r.maybe_rewrite(json.dumps(body).encode(), "/v1/responses")
        return json.loads(raw), meta

    def test_simple_prompt_never_calls_optimizer(self):
        body, meta = self.rewrite("What is 2 + 2?")
        self.assertEqual(Optimizer.requests, [])
        self.assertEqual(body["input"], "What is 2 + 2?")
        self.assertFalse(meta["jev_pre_optimization"]["should_optimize"])

    def test_specialist_used_without_changing_global_or_downstream_model(self):
        body, meta = self.rewrite()
        self.assertEqual(Optimizer.requests[0]["model"], "code-specialist")
        self.assertEqual(self.r.OPT_MODEL, "legacy-model")
        self.assertEqual(body["input"], "Clarified database request.")
        self.assertEqual(body["model"], self.r.MAP["terra"])
        self.assertEqual(meta["jev_pre_optimization"]["selected_model"], "code-specialist")

    def test_metadata_budget_reaches_gate(self):
        body, meta = self.rewrite(metadata={"cost_budget_usd": 0})
        self.assertEqual(Optimizer.requests, [])
        self.assertEqual(body["input"], COMPLEX)

    def test_disabled_policy_can_skip_or_preserve_legacy_model(self):
        self.r.PRE_OPT_CONFIG["enabled"] = False
        self.r.PRE_OPT_CONFIG["disabled"]["should_optimize"] = False
        self.rewrite()
        self.assertEqual(Optimizer.requests, [])
        self.r.PRE_OPT_CONFIG = {}
        self.rewrite()
        self.assertEqual(Optimizer.requests[0]["model"], "legacy-model")

    def test_bad_config_preserves_original_and_classification(self):
        self.r.PRE_OPT_CONFIG = {"enabled": "yes"}
        body, meta = self.rewrite()
        self.assertEqual(body["input"], COMPLEX)
        self.assertEqual(body["model"], self.r.MAP["terra"])
        self.assertTrue(meta["jev_pre_optimization"]["metadata"]["fallback"])
        self.assertEqual(Optimizer.requests, [])

    def test_bad_optimizer_response_preserves_original(self):
        Optimizer.response = {"choices": []}
        body, meta = self.rewrite()
        self.assertEqual(body["input"], COMPLEX)
        self.assertEqual(meta["opt_status"], "FALLBACK_EMPTY_CHOICES")

    def test_continuation_reuses_selection_without_optimization(self):
        self.rewrite()
        Optimizer.requests.clear()
        body, meta = self.rewrite("continue")
        self.assertEqual(Optimizer.requests, [])
        self.assertEqual(meta["tier"], "reuse")
        self.assertEqual(body["model"], self.r.MAP["terra"])

    def test_escalation_still_works_without_optimization(self):
        self.rewrite()
        self.r.ERR_LAST_ASK.clear()
        self.ask.return_value = ("astra", .9, "high", .9)
        Optimizer.requests.clear()
        body = {"model": "incoming", "input": [
            {"role": "user", "content": COMPLEX},
            {"type": "function_call_output", "call_id": "x", "output": "exit code: 1"}]}
        raw, meta = self.r.maybe_rewrite(json.dumps(body).encode(), "/v1/responses")
        self.assertEqual(json.loads(raw)["model"], self.r.MAP["astra"])
        self.assertEqual(Optimizer.requests, [])

    def test_passthrough_unchanged(self):
        raw = b'{"model":"incoming","input":"hello"}'
        self.assertEqual(self.r.maybe_rewrite(raw, "/unrelated")[0], raw)
        self.assertEqual(Optimizer.requests, [])

    def api(self, method, payload):
        handler = self.r.Handler.__new__(self.r.Handler)
        raw = json.dumps(payload).encode()
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.wfile = io.BytesIO()
        handler.send_response = lambda *args: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        getattr(handler, method)()
        return json.loads(handler.wfile.getvalue())

    def test_test_apis_use_same_gate_even_when_force_is_set(self):
        for method in ("_serve_api_test_optimizer", "_serve_api_test_pipeline"):
            with self.subTest(method=method):
                result = self.api(method, {"prompt": COMPLEX, "metadata": {"cost_budget_usd": 0}, "force_optimize": True})
                self.assertEqual(result["optimized_prompt"], COMPLEX)
                self.assertFalse(result["jev_pre_optimization"]["should_optimize"])
        self.assertEqual(Optimizer.requests, [])

    def test_settings_roundtrip_preserves_gate_when_dashboard_omits_it(self):
        with patch.object(self.r, "sync_to_env"):
            ok, _ = self.r.save_settings_data({"pre_optimization": CONFIG})
            self.assertTrue(ok)
            ok, _ = self.r.save_settings_data({"optimizer": {"enabled": True}})
            self.assertTrue(ok)
        saved = json.loads(Path(self.r.SETTINGS_PATH).read_text(encoding="utf-8"))
        self.assertEqual(saved["pre_optimization"], CONFIG)
        self.r.PRE_OPT_CONFIG = {}
        self.r.init_settings()
        self.assertEqual(self.r.get_current_settings()["pre_optimization"], CONFIG)

    def test_invalid_settings_rejected_before_mutation(self):
        with patch.object(self.r, "sync_to_env"):
            ok, _ = self.r.save_settings_data({"optimizer": {"enabled": False}, "pre_optimization": {"enabled": "true"}})
        self.assertFalse(ok)
        self.assertTrue(self.r.OPT_ENABLED)
        self.assertEqual(self.r.PRE_OPT_CONFIG, CONFIG)


if __name__ == "__main__":
    unittest.main()
