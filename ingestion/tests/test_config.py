from pathlib import Path

import pytest

from ingestion.config import load_sources


def write_yaml(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_valid_sources(tmp_path):
    p = write_yaml(tmp_path, """
sources:
  - name: vscode-copilot-issues
    type: github_issues
    repo: microsoft/vscode
    product_area: vscode
    filters:
      labels: [github-copilot]
      state: closed
  - name: teams-qa
    type: manual_qa
    path: ./data/teams_qa/
    product_area: general
""")
    sources = load_sources(p)
    assert len(sources) == 2
    assert sources[0].filters.labels == ["github-copilot"]
    assert sources[1].type == "manual_qa"


def test_unknown_type_raises_with_source_name(tmp_path):
    p = write_yaml(tmp_path, """
sources:
  - name: bad-one
    type: rss_feed
    product_area: general
""")
    with pytest.raises(ValueError, match="bad-one"):
        load_sources(p)


def test_github_issues_requires_repo(tmp_path):
    p = write_yaml(tmp_path, """
sources:
  - name: no-repo
    type: github_issues
    product_area: vscode
""")
    with pytest.raises(ValueError, match="no-repo"):
        load_sources(p)
