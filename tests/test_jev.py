"""Behavioral tests for the pre-optimization gate (no network or live settings)."""
import copy
import importlib.util
import unittest
from unittest.mock import patch


CONFIG = {
    "enabled": True,
    "timeout_ms": 50,
    "disabled": {"should_optimize": True, "selected_model": "general"},
    "fallback": {"should_optimize": False, "selected_model": "general"},
    "criteria": {"min_chars": 40},
    "complexity_keywords": ["refactor", "database", "concurrency"],
    "models": {
        "general": {"estimated_latency_ms": 100, "estimated_cost_usd": 0.001},
        "code-specialist": {"estimated_latency_ms": 500, "estimated_cost_usd": 0.01},
    },
    "routing_rules": [
        {"model": "code-specialist", "when": {"min_complexity": 2}},
        {"model": "general", "when": {}},
    ],
}
COMPLEX = "Refactor the database transaction handler and explain concurrency constraints."


class JevTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("jev"), "The jev module must exist")
        import jev
        self.jev = jev
        self.config = copy.deepcopy(CONFIG)

    def evaluate(self, prompt=COMPLEX, metadata=None):
        return self.jev.JevEvaluator(self.config, default_model="general").evaluate(prompt, metadata)

    def test_simple_prompt_skips_and_returns_normalized_payload(self):
        result = self.evaluate("What is 2 + 2?").to_dict()
        self.assertFalse(result["should_optimize"])
        self.assertIsNone(result["selected_model"])
        self.assertIsInstance(result["reasoning"], str)
        self.assertIsInstance(result["metadata"], dict)

    def test_complex_prompt_routes_to_specialist(self):
        result = self.evaluate()
        self.assertTrue(result.should_optimize)
        self.assertEqual(result.selected_model, "code-specialist")
        self.assertEqual(result.metadata["complexity"], 3)

    def test_budget_excludes_specialist_and_zero_skips_all(self):
        for metadata in ({"latency_budget_ms": 200}, {"cost_budget_usd": 0.002}):
            with self.subTest(metadata=metadata):
                self.assertEqual(self.evaluate(metadata=metadata).selected_model, "general")
        self.assertFalse(self.evaluate(metadata={"cost_budget_usd": 0}).should_optimize)

    def test_category_tier_and_tokens_conditions(self):
        self.config["criteria"] = {"min_tokens": 100, "categories": ["code"], "user_tiers": ["pro"]}
        for metadata, expected in [
            ({"tokens": 100, "category": "code", "user_tier": "pro"}, True),
            ({"tokens": 99, "category": "code", "user_tier": "pro"}, False),
            ({"tokens": 100, "category": "chat", "user_tier": "pro"}, False),
            ({"tokens": 100, "category": "code", "user_tier": "free"}, False),
        ]:
            with self.subTest(metadata=metadata):
                self.assertEqual(self.evaluate(metadata=metadata).should_optimize, expected)

    def test_disabled_policy_and_legacy_default(self):
        self.config["enabled"] = False
        self.assertEqual(self.evaluate("hi").selected_model, "general")
        self.config["disabled"]["should_optimize"] = False
        self.assertFalse(self.evaluate().should_optimize)
        result = self.jev.JevEvaluator(None, default_model="legacy").evaluate("hi")
        self.assertEqual(result.selected_model, "legacy")
        self.assertTrue(result.should_optimize)

    def test_unmatched_rules_skip(self):
        self.config["routing_rules"] = []
        self.assertFalse(self.evaluate().should_optimize)

    def test_invalid_config_fails_closed(self):
        for key, value in [("enabled", "false"), ("timeout_ms", -1),
                           ("models", {}), ("criteria", {"min_toknes": 1}),
                           ("fallback", {"should_optimize": True, "selected_model": "unknown"})]:
            with self.subTest(key=key):
                config = copy.deepcopy(CONFIG)
                config[key] = value
                with self.assertRaises(ValueError):
                    self.jev.JevConfig.from_dict(config, "general")
                result = self.jev.JevEvaluator(config, "general").evaluate(COMPLEX)
                self.assertFalse(result.should_optimize)
                self.assertTrue(result.metadata["fallback"])

    def test_invalid_metadata_uses_configured_fallback(self):
        self.config["fallback"]["should_optimize"] = True
        for metadata in ({"tokens": -1}, {"tokens": True}, []):
            with self.subTest(metadata=metadata):
                result = self.evaluate(metadata=metadata)
                self.assertTrue(result.should_optimize)
                self.assertEqual(result.selected_model, "general")
                self.assertTrue(result.metadata["fallback"])

    def test_invalid_budget_cannot_enable_paid_fallback(self):
        self.config["fallback"]["should_optimize"] = True
        self.assertFalse(self.evaluate(metadata={"cost_budget_usd": float("nan")}).should_optimize)

    def test_timeout_exception_and_invalid_result_use_fallback(self):
        evaluator = self.jev.JevEvaluator(self.config, "general")
        for error in (TimeoutError(), RuntimeError("private prompt data")):
            with patch.object(evaluator, "_evaluate", side_effect=error):
                result = evaluator.evaluate(COMPLEX)
                self.assertFalse(result.should_optimize)
                self.assertTrue(result.metadata["fallback"])
                self.assertNotIn("private prompt", result.reasoning)
        with patch.object(evaluator, "_evaluate", return_value={"should_optimize": "true"}):
            self.assertTrue(evaluator.evaluate(COMPLEX).metadata["fallback"])

    def test_deadline_exhaustion_skips_optimization(self):
        with patch("jev.time.monotonic", side_effect=[0, 1, 1, 1, 1]):
            result = self.evaluate()
        self.assertFalse(result.should_optimize)
        self.assertTrue(result.metadata["fallback"])

    def test_budget_still_applies_to_disabled_policy(self):
        self.config["enabled"] = False
        self.assertFalse(self.evaluate(metadata={"cost_budget_usd": 0}).should_optimize)

    def test_configured_budget_cannot_be_relaxed_by_request(self):
        self.config["cost_budget_usd"] = 0.002
        self.assertEqual(self.evaluate(metadata={"cost_budget_usd": 1}).selected_model, "general")
        self.config["latency_budget_ms"] = 50
        self.assertFalse(self.evaluate().should_optimize)

    def test_unknown_estimates_cannot_meet_a_budget(self):
        self.config["models"] = {"general": {}, "code-specialist": {}}
        self.assertFalse(self.evaluate(metadata={"latency_budget_ms": 1000}).should_optimize)

    def test_typed_config_can_be_used_directly(self):
        config = self.jev.JevConfig.from_dict(CONFIG, "general")
        self.assertEqual(self.jev.JevEvaluator(config, "general").evaluate(COMPLEX).selected_model, "code-specialist")

    def test_malformed_model_reference_rejected_as_validation_error(self):
        self.config["fallback"]["selected_model"] = []
        with self.assertRaises(ValueError):
            self.jev.JevConfig.from_dict(self.config, "general")


if __name__ == "__main__":
    unittest.main()
