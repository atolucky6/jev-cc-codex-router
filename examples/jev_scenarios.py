"""Run from any directory: python examples/jev_scenarios.py. No API calls."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jev import JevEvaluator


def main():
    config = json.loads((ROOT / "jev.example.json").read_text(encoding="utf-8"))
    evaluator = JevEvaluator(config)
    scenarios = [
        ("Simple prompt", "What is 2 + 2?", {}),
        ("Coding specialist", "Refactor the database transaction handler and explain concurrency constraints.",
         {"category": "code", "user_tier": "pro", "tokens": 100}),
    ]
    for name, prompt, metadata in scenarios:
        print(name)
        print(json.dumps(evaluator.evaluate(prompt, metadata).to_dict(), indent=2))


if __name__ == "__main__":
    main()
