# Development

This repo is a `uv` workspace with two members: `shared` and `ingestion`.

## Setup

```bash
uv sync --all-packages
```

Plain `uv sync` only installs the root project — it will **not** install the
`shared` and `ingestion` workspace member packages into the venv, so their
imports (`ingestion.*`, `advisor_shared.*`) will fail. Always use
`--all-packages`.

## Corporate networks

If PyPI is blocked or slow on your network, set `UV_INDEX_URL` to point at
your corporate PyPI proxy before running `uv sync`.

## 图片输入的容量影响

单个满配图片请求(4 张 × 4MB,limits 见 `channels/teams/src/teams_adapter/downloader.py`)
在飞行中同时驻留:

| 部分 | 峰值 |
|------|------|
| 原始字节(`request.images`,core.handle 栈帧持有) | ~16 MB |
| base64 字符串(`messages` 里的 data URL) | ~21 MB |
| openai SDK 序列化的 JSON body(`httpx.Request.content`) | ~21 MB |
| **合计** | **~58 MB / 并发请求** |

aiohttp 单进程多并发,该数字乘并发数。据此设定 Teams 侧并发上限与容器内存。

注:`AdvisorCore` 的重试是**串行**的,上一次 `run` 的栈帧已退出,**不叠加**内存;
重试代价是 CPU(约 16MB 的 base64 重编码,一二十毫秒)。

已知未闭合项(见实现计划的「后续任务」小节):`DOWNLOAD_TIMEOUT_S` 是
**每阶段**超时而非总时长,慢速滴送的服务器能把一个 turn 拖住任意久;
且大小上限是**读完整个 body 之后**才判的。两者的修法是同一处改动
(流式截断 + 下载总预算),已记录待办。
