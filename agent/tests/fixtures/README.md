# eval 图片 fixture

`test_eval_behavior.py` 的两条图片用例需要真实截图。**不要用合成图**——
这两条用例断言的正是模型真实的 OCR 与理解质量,合成图测不出来。

缺失时对应用例自动 skip,不会误报失败。

| 文件 | 内容要求 | 对应用例 |
|------|---------|---------|
| `error_screenshot.png` | VS Code 中 Copilot 的错误提示,**须含清晰可读的英文原文 `GitHub Copilot: Authentication failed`** | `image-auth-error-screenshot` |
| `config_screenshot.png` | VS Code `settings.json` 中的 Copilot 配置片段,**须含清晰可读的 `github.copilot.enable` 这一行** | `image-config-screenshot-no-text` |

每张控制在 100 KB 内。格式必须是 PNG(用例的 `mime_type` 写死为 `image/png`)。

截好后跑:
`uv run --env-file .env pytest -m integration agent/tests/test_eval_behavior.py -v`

若断言失败,先人工看 `resp.markdown` 判断是模型没读出文本(需换更清晰的截图)
还是关键词选得太窄(调整 `expect_answer_contains_any`)。
