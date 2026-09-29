[English](README.md) | **简体中文**

# jev-cc-codex-router

一个单文件 Python 代理：把 Codex 的每个任务，路由到够用的最便宜的模型档位。

它放在 Codex 和任意兼容 OpenAI **Responses API** 的上游之间（本地网关如 cc-switch，或远程地址）。每来一个新的用户任务，它先问 [Jev](https://docs.typesafe.ai)（TypeSafe System One）这个任务需要哪一档，然后改写请求里的 `model` 字段，本轮后面的请求沿用这个选择。

> 状态：早期版本。在作者自己的环境里能用，档位判断的提示词还在调。有一份小样本的自测成本估算，包含测试条件和局限，见 [BENCHMARK.zh-CN.md](BENCHMARK.zh-CN.md)。请不要期待具体的节省比例。

## 判断逻辑

- **新的用户任务：** 把当前这句话加最近的对话（最多 50 轮、约 4200 字符，去掉工具调用和系统提示）发给 Jev，要求在 `luna`、`terra`、`sol`、`astra` 四档里选一档。档位映射到模型名（可配置），并按会话保存。
- **本轮后面的请求：** 工具结果之后的续接请求沿用已保存的模型，不会额外调用 Jev。
- **纯接续词**（"继续"、"retry"、"continue" 等）直接沿用上一档，不问 Jev。
- **报错升档：** 如果工具输出里有明显的失败（Traceback、非 0 的退出码、`command not found`、`N failed`、合并冲突等），代理会带上任务和报错再问 Jev 一次。只有 Jev 选了更高的档才切换，不会降档，同一会话 20 秒内最多问一次。
- **兜底：** Jev 返回未知档位或调用失败时，请求用兜底模型（默认 `gpt-5.6-sol`）。
- **不处理的请求：** Codex 自己的辅助请求（生成标题、记忆总结）以及模型与该会话基准模型不同的请求，原样透传。
- 会话由请求头 `session-id`（或 `thread-id`）识别。重启后代理不认识任何会话，请求会先原样透传，直到下一个新任务出现。

## 其他行为

- 对 `POST /responses`，上游返回 400/502/503/504 或连接失败时，最多重试 2 次，每次等 1 秒，用来掩盖上游偶发的错误。它无法恢复 200 之后流被中途切断的情况。
- 支持 `Content-Encoding: zstd` 的请求体（解压、改写、再压缩）。Python 3.14 及以上用标准库；更老的 Python 需要 `pip install zstandard`。
- **调试输出默认关闭。** 设置 `JEV_DEBUG=1` 才会写 `decisions.jsonl`（每个请求的档位、概率、入口和出口模型）、`req-headers.jsonl`（请求头）和 `errors-400/`（失败的 400 请求全文）。这些文件含有提问内容，请保密，它们已被 `.gitignore` 排除。关闭调试时，代理不写任何文件。

## 安装

1. Python 3.9 及以上（内置 zstd 需 3.14）。
2. `cp .env.example .env`，填入 `TYPESAFE_API_KEY`（你自己的 TypeSafe key）。
3. 上游不是 `http://127.0.0.1:15721` 时，在 `.env` 里设置：`JEV_UPSTREAM=http://your-gateway:port`
4. 运行：`python3 jev_router.py`
5. 在 `~/.codex/config.toml` 里让 Codex 指向代理：

```toml
model_provider = "jevrouter"

[model_providers.jevrouter]
name = "Jev Router"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
```

provider 的其他字段（鉴权、`requires_openai_auth` 等）按你连接上游的方式调整。代理会原样保留 Codex 发出的请求头，所以鉴权信息会不变地转发给上游。

回滚只要把 `model_provider` 改回原来的 provider。代理不保存持久状态。

## 配置

所有设置都是环境变量（或 `.env` 里的行），完整列表见 `.env.example`：监听地址、上游、Jev API 地址、每档对应的模型名、兜底模型、重试次数和间隔、日志路径，以及 `JEV_REWRITE=0`（关闭路由，只做代理和重试）。

## 说明与局限

- 每个新任务多一次小的 Jev 调用（失败步骤最多再多一次）。延迟大致是 Jev 的响应时间，最慢几秒，超时 15 秒。
- 档位定义在 `ask_jev()` 里，请按自己的工作改。目前反映的是编码代理类的工作负载。部分提示词和接续词表刻意用中文（作者用中文工作），英文输入同样可用。
- 模型名默认是 `gpt-5.6-luna/terra/sol` 和 `gpt-6-astra`，请改成你的上游实际提供的名字。
- 与 TypeSafe、OpenAI 无关联。

## 许可证

MIT
