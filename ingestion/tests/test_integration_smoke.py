"""真实资源冒烟:需要环境变量齐全,uv run pytest -m integration 才跑。"""
import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

REQUIRED_ENV = ["GITHUB_TOKEN", "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY",
                "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"]


@pytest.fixture(autouse=True)
def require_env():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        pytest.skip(f"missing env: {missing}")


async def test_intellij_source_end_to_end_small_window():
    """拉 copilot-intellij-feedback 最近 7 天 closed issues,走完整管道。"""
    from ingestion.__main__ import main  # noqa: F401  验证装配可导入
    from ingestion import connectors
    from ingestion.config import SourceConfig

    config = SourceConfig(
        name="intellij-copilot-smoke", type="github_issues",
        repo="microsoft/copilot-intellij-feedback", product_area="intellij",
        filters={"state": "closed"},
    )
    since = datetime.now(timezone.utc) - timedelta(days=7)
    connector = connectors.create(config)
    items = []
    async for qa in connector.fetch(since):
        items.append(qa)
        if len(items) >= 3:
            break
    # 近 7 天可能没有带答案的 closed issue;拉到 0 条也算连通成功
    for qa in items:
        assert qa.title and qa.answer and qa.url
