"""Configurable, local pre-optimization gate. Uses only the Python standard library."""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


def _object(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed) - {"_comment"}:
        raise ValueError("Expected an object with supported configuration fields")
    return {k: v for k, v in value.items() if k != "_comment"}


def _number(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value)) or value < 0):
        raise ValueError("Expected a finite non-negative number")
    return value


def _strings(value):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError("Expected a list of non-empty strings")
    return value


@dataclass(frozen=True)
class Conditions:
    min_chars: int = 0
    min_tokens: int = 0
    min_complexity: int = 0
    categories: list[str] = field(default_factory=list)
    user_tiers: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data):
        values = _object(data, cls.__dataclass_fields__)
        for key, value in values.items():
            if key.startswith("min_"):
                _number(value)
                if not isinstance(value, int):
                    raise ValueError("Thresholds must be integers")
            else:
                _strings(value)
        return cls(**values)

    def matches(self, facts):
        return (facts["chars"] >= self.min_chars
                and facts["tokens"] >= self.min_tokens
                and facts["complexity"] >= self.min_complexity
                and (not self.categories or facts.get("category") in self.categories)
                and (not self.user_tiers or facts.get("user_tier") in self.user_tiers))


@dataclass(frozen=True)
class ModelConfig:
    estimated_latency_ms: Optional[float] = None
    estimated_cost_usd: Optional[float] = None


@dataclass(frozen=True)
class Policy:
    should_optimize: bool
    selected_model: str

    @classmethod
    def from_dict(cls, data, models):
        values = _object(data, cls.__dataclass_fields__)
        if (type(values.get("should_optimize")) is not bool
                or not isinstance(values.get("selected_model"), str) or values["selected_model"] not in models):
            raise ValueError("Policy requires a boolean and a configured model")
        return cls(**values)


@dataclass(frozen=True)
class RoutingRule:
    model: str
    when: Conditions


@dataclass(frozen=True)
class JevConfig:
    enabled: bool
    timeout_ms: float
    disabled: Policy
    fallback: Policy
    criteria: Conditions
    complexity_keywords: list[str]
    models: dict[str, ModelConfig]
    routing_rules: list[RoutingRule]
    latency_budget_ms: Optional[float] = None
    cost_budget_usd: Optional[float] = None

    @classmethod
    def from_dict(cls, data, default_model: str) -> JevConfig:
        values = _object({} if data is None else data, cls.__dataclass_fields__)
        enabled = values.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        timeout = _number(values.get("timeout_ms", 50))
        if timeout == 0:
            raise ValueError("timeout_ms must be positive")
        raw_models = values.get("models", {default_model: {}})
        if not isinstance(raw_models, dict) or not raw_models:
            raise ValueError("At least one model is required")
        models = {}
        for name, raw in raw_models.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Model names must be non-empty strings")
            estimates = _object(raw, ModelConfig.__dataclass_fields__)
            for estimate in estimates.values():
                if estimate is not None:
                    _number(estimate)
            models[name] = ModelConfig(**estimates)
        policy_model = default_model if default_model in models else next(iter(models))
        disabled = Policy.from_dict(values.get("disabled", {
            "should_optimize": True, "selected_model": policy_model}), models)
        fallback = Policy.from_dict(values.get("fallback", {
            "should_optimize": False, "selected_model": policy_model}), models)
        criteria = Conditions.from_dict(values.get("criteria", {"min_chars": 80}))
        keywords = _strings(values.get("complexity_keywords", ["refactor", "architecture", "database", "concurrency"]))
        raw_rules = values.get("routing_rules", [{"model": policy_model, "when": {}}])
        if not isinstance(raw_rules, list):
            raise ValueError("routing_rules must be a list")
        rules = []
        for raw in raw_rules:
            rule = _object(raw, ("model", "when"))
            if not isinstance(rule.get("model"), str) or rule["model"] not in models:
                raise ValueError("Routing rule references an unknown model")
            rules.append(RoutingRule(rule["model"], Conditions.from_dict(rule.get("when", {}))))
        budgets = {key: values.get(key) for key in ("latency_budget_ms", "cost_budget_usd")}
        for value in budgets.values():
            if value is not None:
                _number(value)
        return cls(enabled, timeout, disabled, fallback, criteria, keywords, models, rules, **budgets)


@dataclass(frozen=True)
class JevDecision:
    should_optimize: bool
    selected_model: Optional[str]
    reasoning: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class JevEvaluator:
    """Ordered rules select the first matching model within estimated budgets.

    The local evaluator checks a cooperative deadline between rules/keywords;
    there is no network call or background worker to hang after a timeout.
    """

    def __init__(self, config=None, default_model: str = "gemini-3.8-flash-high"):
        self.config = None
        try:
            if isinstance(config, JevConfig):
                config = asdict(config)
            self.config = JevConfig.from_dict(config, default_model)
        except (ValueError, TypeError):
            pass  # Untrusted config cannot supply a trustworthy fallback policy.

    @staticmethod
    def _deadline(deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError()

    def _affordable(self, model, facts):
        estimates = self.config.models[model]
        for budget, estimate in (("latency_budget_ms", estimates.estimated_latency_ms),
                                 ("cost_budget_usd", estimates.estimated_cost_usd)):
            limits = [limit for limit in (getattr(self.config, budget), facts.get(budget)) if limit is not None]
            if limits and (estimate is None or estimate > min(limits)):
                return False
        return True

    def _policy(self, policy, reason, facts):
        if policy.should_optimize and self._affordable(policy.selected_model, facts):
            return JevDecision(True, policy.selected_model, reason, facts)
        return JevDecision(False, None, reason + "; optimization skipped", facts)

    def _evaluate(self, prompt, metadata, deadline):
        self._deadline(deadline)
        if not isinstance(prompt, str) or not isinstance(metadata, dict):
            raise ValueError("Invalid evaluation input")
        facts = {"chars": len(prompt), "tokens": max(1, math.ceil(len(prompt) / 3.8)), "complexity": 0}
        for key in ("tokens", "latency_budget_ms", "cost_budget_usd"):
            if key in metadata:
                facts[key] = _number(metadata[key])
        for key in ("category", "user_tier"):
            if key in metadata:
                if not isinstance(metadata[key], str):
                    raise ValueError("Expected string context")
                facts[key] = metadata[key]
        if not self.config.enabled:
            return self._policy(self.config.disabled, "Jev disabled; configured policy", facts)
        lowered = prompt.lower()
        for keyword in set(k.lower() for k in self.config.complexity_keywords):
            self._deadline(deadline)
            facts["complexity"] += int(keyword in lowered)
        if not self.config.criteria.matches(facts):
            return JevDecision(False, None, "Prompt does not meet optimization criteria", facts)
        for index, rule in enumerate(self.config.routing_rules):
            self._deadline(deadline)
            if rule.when.matches(facts) and self._affordable(rule.model, facts):
                return JevDecision(True, rule.model, f"Routing rule {index + 1} matched within estimated budgets", facts)
        return JevDecision(False, None, "No matching model within estimated budgets", facts)

    def evaluate(self, prompt: str, metadata: Optional[dict] = None) -> JevDecision:
        start = time.monotonic()
        if self.config is None:
            return JevDecision(False, None, "Invalid Jev configuration; optimization skipped", {"fallback": True})
        try:
            deadline = start + self.config.timeout_ms / 1000
            result = self._evaluate(prompt, {} if metadata is None else metadata, deadline)
            self._deadline(deadline)
            if (not isinstance(result, JevDecision) or type(result.should_optimize) is not bool
                    or not isinstance(result.reasoning, str) or not isinstance(result.metadata, dict)
                    or (result.should_optimize and result.selected_model not in self.config.models)
                    or (not result.should_optimize and result.selected_model is not None)):
                raise ValueError("Invalid evaluation result")
        except Exception as exc:
            # Keep valid budget limits even if another context field is invalid.
            budgets = {}
            if isinstance(metadata, dict):
                for key in ("latency_budget_ms", "cost_budget_usd"):
                    if key in metadata:
                        try:
                            budgets[key] = _number(metadata[key])
                        except ValueError:
                            budgets[key] = 0
            result = self._policy(self.config.fallback, f"Jev fallback: {type(exc).__name__}", budgets)
            result.metadata["fallback"] = True
        result.metadata["evaluation_ms"] = round((time.monotonic() - start) * 1000, 3)
        return result
