**English** | [简体中文](BENCHMARK.zh-CN.md)

# Cost estimate from one real session (early, not a benchmark)

This is a single, small, self-measured sample. Treat it as a rough indication, not as a general result.

## Test conditions

- **Window:** 2026-09-29 17:43 to 22:31 (Asia/Shanghai), about 4 h 48 min, starting when the routing version with history-aware tier selection went live.
- **Sample:** 304 successful Codex requests from one user, of which 22 were new user turns graded by Jev. Requests that failed and were retried are not counted.
- **Data sources:** `decisions.jsonl` (routing decisions written by this proxy when `JEV_DEBUG=1`) and the request log of the local gateway (cc-switch), which records model, token counts (fresh input, cache read, output) and cost per request.
- **Prices used (USD per million tokens):**

| Model | Fresh input | Cache read | Output |
|---|---|---|---|
| gpt-5.6-luna | 0.2 | 0.02 | 1.2 |
| gpt-5.6-terra | 2.0 | 0.2 | 12.0 |
| gpt-5.6-sol | 4.0 | 0.4 | 20.0 |

  These come from the gateway's price table and may differ from what an upstream actually bills. Recomputing cost with these prices matches the gateway's recorded total (23.23 USD).
- **Baseline:** the same 304 requests, keeping their real token counts and cache hits, priced as if every request had run on `gpt-5.6-sol`.

## Results

| | Requests | Actual cost | Priced as all-sol |
|---|---|---|---|
| sol | 221 | 19.85 | 19.85 |
| terra | 51 | 3.24 | 6.44 |
| luna | 32 | 0.14 | 2.63 |
| **Total** | **304** | **23.23** | **28.92** |

- Estimated saving: about **19.7%** versus all-sol.
- New-turn tiers chosen by Jev: sol 14, terra 5, luna 3.
- Cache hit rate by model: sol 94%, terra 94%, luna 89%. Switching models did not visibly reduce cache hits.
- Failure escalation: 9 checks, 0 upgrades.
- About 85% of the spend is still on sol. The saving comes mostly from the 32 luna requests.

## Limitations

1. Only 22 graded turns, one user, one afternoon. Another workload can look very different.
2. The baseline is an estimate, not a real A/B run. A cheaper model may need more turns or tokens for the same task, which this estimate ignores, so the real saving may be smaller.
3. No quality measurement: it does not tell whether tasks on luna or terra were done correctly or needed rework.
4. Prices may not match actual billing.
5. Latency was not compared. Average whole-request time (streaming included) was luna 38 s, terra 25 s, sol 17 s, but task types differ, so these are not comparable.

## Reproduce

1. Run the proxy with `JEV_DEBUG=1` so `decisions.jsonl` is written, then read it for routed turns (`new`, `tier`, `out`).
2. From the gateway's request log, take successful requests in the same window and sum fresh input (input minus cache read), cache read and output tokens per model.
3. Price them with the table above, then price the same tokens at sol rates and compare.
