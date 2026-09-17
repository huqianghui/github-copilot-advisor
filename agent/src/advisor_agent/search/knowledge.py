"""KB 检索:AI Search hybrid(BM25+向量+semantic ranker)(spec 7.2)。"""
from azure.search.documents.models import VectorizedQuery

from advisor_agent.llm_diagnostics import sdk_call_diagnostics
from advisor_agent.search.models import MIN_RERANKER_SCORE, SearchResult
from advisor_shared.telemetry import step


class KnowledgeSearchClient:
    def __init__(self, search_client, embed_client,
                 embed_model: str = "text-embedding-3-large"):
        self.search_client = search_client
        self.embed = embed_client
        self.embed_model = embed_model

    async def search(self, query: str, product_area: str | None = None,
                     top: int = 5) -> list[SearchResult]:
        with step("search.kb.embedding", model=self.embed_model):
            with sdk_call_diagnostics("embeddings") as diagnostics:
                emb = await self.embed.embeddings.create(model=self.embed_model,
                                                         input=[query])
                if diagnostics is not None:
                    diagnostics.record_usage(getattr(emb, "usage", None))
        vector = VectorizedQuery(vector=emb.data[0].embedding,
                                 k_nearest_neighbors=top,
                                 fields="content_vector")
        odata_filter = None
        if product_area:
            safe = product_area.replace("'", "''")
            odata_filter = f"product_area eq '{safe}'"
        with step("search.kb.azure_search", top=top,
                  query_type="hybrid_semantic", has_filter=bool(odata_filter)):
            pager = await self.search_client.search(
                search_text=query,
                vector_queries=[vector],
                query_type="semantic",
                semantic_configuration_name="default",
                filter=odata_filter,
                top=top,
            )
            # The SDK sends requests while consuming the lazy pager, not at search().
            documents = [document async for document in pager]
        with step("search.kb.filter") as timing:
            results = []
            for d in documents:
                score = d.get("@search.reranker_score") or 0.0
                if score < MIN_RERANKER_SCORE:
                    continue
                results.append(SearchResult(
                    title=d["title"], content=d["content"], url=d["url"],
                    origin="kb", score=score))
            timing.attributes.update(
                raw_count=len(documents), result_count=len(results),
                filtered_count=len(documents) - len(results))
            timing.status = "success" if results else "empty"
        return results
