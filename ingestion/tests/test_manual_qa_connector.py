from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingestion.config import SourceConfig
from ingestion.connectors import create
from ingestion.connectors.manual_qa import ManualQAConnector

QA_MD = """---
title: Copilot 在企业代理下无法登录
url: https://teams.example.com/thread/123
labels: [network, auth]
created_at: 2026-03-01
---
## Question
公司代理环境下 VS Code Copilot 登录一直转圈。

## Answer
设置 http.proxy 并将 *.githubcopilot.com 加入代理白名单后解决。
"""


@pytest.fixture
def qa_dir(tmp_path: Path) -> Path:
    d = tmp_path / "teams_qa"
    d.mkdir()
    (d / "proxy-login.md").write_text(QA_MD, encoding="utf-8")
    return d


def make_config(path: Path) -> SourceConfig:
    return SourceConfig(
        name="teams-qa", type="manual_qa", path=str(path), product_area="general"
    )


async def collect(connector, since=None):
    return [qa async for qa in connector.fetch(since)]


async def test_parses_frontmatter_and_sections(qa_dir):
    items = await collect(ManualQAConnector(make_config(qa_dir)))
    assert len(items) == 1
    qa = items[0]
    assert qa.native_id == "proxy-login.md"
    assert qa.title == "Copilot 在企业代理下无法登录"
    assert "转圈" in qa.question
    assert "白名单" in qa.answer
    assert qa.labels == ["network", "auth"]
    assert qa.doc_type == "manual_qa"
    assert qa.created_at == datetime(2026, 3, 1, tzinfo=timezone.utc)


async def test_since_filters_older_files(qa_dir):
    items = await collect(
        ManualQAConnector(make_config(qa_dir)),
        since=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    assert items == []


async def test_skips_file_missing_answer_section(qa_dir):
    (qa_dir / "bad.md").write_text(
        "---\ntitle: t\ncreated_at: 2026-03-01\n---\n## Question\nq only\n",
        encoding="utf-8",
    )
    items = await collect(ManualQAConnector(make_config(qa_dir)))
    assert len(items) == 1  # bad.md 被跳过


def test_factory_dispatches_by_type(qa_dir):
    assert isinstance(create(make_config(qa_dir)), ManualQAConnector)
