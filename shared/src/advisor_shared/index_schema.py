# shared/src/advisor_shared/index_schema.py
"""Azure AI Search 索引定义 — 字段名与 semantic 槽位一一对应(spec 6.2)。"""


def _field(name: str, type_: str = "Edm.String", *, key: bool = False,
           searchable: bool = False, filterable: bool = False,
           facetable: bool = False, sortable: bool = False,
           retrievable: bool = True) -> dict:
    return {
        "name": name, "type": type_, "key": key, "searchable": searchable,
        "filterable": filterable, "facetable": facetable,
        "sortable": sortable, "retrievable": retrievable,
    }


def build_index_definition(name: str, vector_dimensions: int = 3072) -> dict:
    fields = [
        _field("id", key=True, filterable=True),
        _field("title", searchable=True),
        _field("content", searchable=True),
        {**_field("keywords", "Collection(Edm.String)", searchable=True,
                  filterable=True, facetable=True)},
        _field("raw_content", searchable=True, retrievable=False),
        _field("url"),
        _field("source", filterable=True, facetable=True),
        _field("doc_type", filterable=True),
        _field("product_area", filterable=True, facetable=True),
        _field("created_at", "Edm.DateTimeOffset", filterable=True, sortable=True),
        _field("resolved_at", "Edm.DateTimeOffset", filterable=True, sortable=True),
        {
            "name": "content_vector", "type": "Collection(Edm.Single)",
            "searchable": True, "retrievable": False,
            "dimensions": vector_dimensions,
            "vectorSearchProfile": "vprofile",
        },
    ]
    return {
        "name": name,
        "fields": fields,
        "vectorSearch": {
            "algorithms": [{"name": "hnsw", "kind": "hnsw"}],
            "profiles": [{"name": "vprofile", "algorithm": "hnsw"}],
        },
        "semantic": {
            "configurations": [{
                "name": "default",
                "prioritizedFields": {
                    "titleField": {"fieldName": "title"},
                    "prioritizedContentFields": [{"fieldName": "content"}],
                    "prioritizedKeywordsFields": [{"fieldName": "keywords"}],
                },
            }]
        },
    }
