"""KB 检索:AI Search hybrid(BM25+向量+semantic ranker)(spec 7.2)。"""
from azure.search.documents.models import VectorizedQuery

from advisor_agent.search.models import MIN_RERANKER_SCORE, SearchResult


class KnowledgeSearchClient:
    def __init__(self, search_client, embed_client,
                 embed_model: str = "text-embedding-3-large"):
        self.search_client = search_client
        self.embed = embed_client
        self.embed_model = embed_model

    async def search(self, query: str, product_area: str | None = None,
                     top: int = 5) -> list[SearchResult]:
        emb = await self.embed.embeddings.create(model=self.embed_model,
                                                 input=[query])
        vector = VectorizedQuery(vector=emb.data[0].embedding,
                                 k_nearest_neighbors=top,
                                 fields="content_vector")
        pager = await self.search_client.search(
            search_text=query,
            vector_queries=[vector],
            query_type="semantic",
            semantic_configuration_name="default",
            filter=f"product_area eq '{product_area}'" if product_area else None,
            top=top,
        )
        results = []
        async for d in pager:
            score = d.get("@search.reranker_score") or 0.0
            if score < MIN_RERANKER_SCORE:
                continue
            results.append(SearchResult(
                title=d["title"], content=d["content"], url=d["url"],
                origin="kb", score=score))
        return results
