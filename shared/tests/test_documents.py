# shared/tests/test_documents.py
from datetime import datetime, timezone

from advisor_shared.documents import QADocument, RAW_CONTENT_MAX_CHARS
from advisor_shared.index_schema import build_index_definition


def make_doc(**overrides) -> QADocument:
    defaults = dict(
        id=QADocument.make_id("vscode", "12345"),
        title="Copilot chat times out",
        content="升级到最新版插件并检查代理设置。",
        keywords=["github-copilot", "network"],
        raw_content="original thread text " * 10,
        url="https://github.com/microsoft/vscode/issues/12345",
        source="vscode-copilot-issues",
        doc_type="issue",
        product_area="vscode",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        resolved_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return QADocument(**defaults)


def test_make_id_is_deterministic_32_hex():
    a = QADocument.make_id("vscode", "12345")
    b = QADocument.make_id("vscode", "12345")
    assert a == b and len(a) == 32 and int(a, 16) is not None


def test_make_id_differs_by_source():
    assert QADocument.make_id("a", "1") != QADocument.make_id("b", "1")


def test_embedding_text_is_title_newline_content():
    doc = make_doc()
    assert doc.embedding_text() == doc.title + "\n" + doc.content


def test_to_search_document_serializes_datetimes_and_omits_none_vector():
    d = make_doc().to_search_document()
    assert d["created_at"] == "2026-01-01T00:00:00+00:00"
    assert "content_vector" not in d
    doc2 = make_doc(content_vector=[0.1] * 4)
    assert doc2.to_search_document()["content_vector"] == [0.1] * 4


def test_raw_content_max_chars_constant():
    assert RAW_CONTENT_MAX_CHARS == 32_000


def test_index_definition_matches_spec_fields():
    idx = build_index_definition("qa-index")
    fields = {f["name"]: f for f in idx["fields"]}
    assert set(fields) == {
        "id", "title", "content", "keywords", "raw_content", "url",
        "source", "doc_type", "product_area", "created_at", "resolved_at",
        "content_vector",
    }
    assert fields["id"]["key"] is True
    assert fields["raw_content"]["searchable"] is True
    assert fields["raw_content"]["retrievable"] is False
    assert fields["keywords"]["filterable"] is True
    assert fields["content_vector"]["dimensions"] == 3072
    sem = idx["semantic"]["configurations"][0]["prioritizedFields"]
    assert sem["titleField"]["fieldName"] == "title"
    assert sem["prioritizedContentFields"][0]["fieldName"] == "content"
    assert sem["prioritizedKeywordsFields"][0]["fieldName"] == "keywords"
