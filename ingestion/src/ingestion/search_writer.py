# ingestion/src/ingestion/search_writer.py
"""embedding + AI Search 幂等写入(spec 6.4)。"""
import logging

from azure.core.exceptions import HttpResponseError
from azure.search.documents.indexes.models import SearchIndex

from advisor_shared.documents import QADocument
from advisor_shared.index_schema import build_index_definition

logger = logging.getLogger(__name__)

_EMBED_BATCH = 16


def ensure_index(search_index_client, index_name: str) -> None:
    definition = build_index_definition(index_name)
    try:
        search_index_client.create_index(SearchIndex(definition))
        logger.info("created index %s", index_name)
    except HttpResponseError as e:
        if e.status_code == 409 or "already exists" in str(e).lower():
            return
        raise


class SearchWriter:
    def __init__(self, search_client, embed_client,
                 embed_model: str = "text-embedding-3-large"):
        self.search = search_client
        self.embed = embed_client
        self.embed_model = embed_model

    async def upsert(self, docs: list[QADocument]) -> int:
        if not docs:
            return 0
        for i in range(0, len(docs), _EMBED_BATCH):
            batch = docs[i:i + _EMBED_BATCH]
            resp = await self.embed.embeddings.create(
                model=self.embed_model,
                input=[d.embedding_text() for d in batch],
            )
            for doc, item in zip(batch, resp.data):
                doc.content_vector = item.embedding
        results = await self.search.merge_or_upload_documents(
            documents=[d.to_search_document() for d in docs])
        return sum(1 for r in results if r.succeeded)
