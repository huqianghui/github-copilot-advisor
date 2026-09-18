"""Keep all test-owned filesystem work under the ignored project output tree."""

import shutil
from pathlib import Path
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def tmp_path():
    directory = ROOT / "deploy" / "out" / "package-tests" / uuid4().hex
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


@pytest.fixture
def config_case(tmp_path):
    (tmp_path / "approved").mkdir()
    (tmp_path / "approved" / "escalation.yaml").write_text("contacts: []\n", encoding="utf-8")
    (tmp_path / "approved" / "diagnostics.yaml").write_text("checks: []\n", encoding="utf-8")
    values = {
        "AZURE_SUBSCRIPTION_ID": "18827cdf-a4e8-4316-a7ba-a4499139a3ec",
        "AZURE_OPENAI_RESOURCE_ID": "/subscriptions/18827cdf-a4e8-4316-a7ba-a4499139a3ec/resourceGroups/rg-dependencies/providers/Microsoft.CognitiveServices/accounts/model",
        "AZURE_SEARCH_RESOURCE_ID": "/subscriptions/18827cdf-a4e8-4316-a7ba-a4499139a3ec/resourceGroups/rg-dependencies/providers/Microsoft.Search/searchServices/search",
        "AZURE_OPENAI_ENDPOINT": "https://model.openai.azure.com",
        "AZURE_SEARCH_ENDPOINT": "https://search.search.windows.net",
        "AZURE_OPENAI_CHAT_DEPLOYMENT": "approved-chat",
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "text-embedding-3-large",
        "AZURE_SEARCH_INDEX": "copilot-qa",
        "ESCALATION_CONFIG": "approved/escalation.yaml",
        "DIAGNOSTICS_CONFIG": "approved/diagnostics.yaml",
    }

    def write(**overrides):
        entries = values | overrides
        path = tmp_path / "fixture.env"
        path.write_text(
            "\n".join(f"{name}='{value}'" for name, value in entries.items() if value is not None) + "\n",
            encoding="utf-8",
        )
        return path

    return write
