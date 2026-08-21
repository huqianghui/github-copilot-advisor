from datetime import datetime, timezone

from ingestion.config import SourceConfig
from ingestion.connectors.base import Connector, RawQA
from ingestion.pipeline import run_pipeline
from ingestion.watermark import WatermarkStore

NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)


def make_config(name: str) -> SourceConfig:
    return SourceConfig(name=name, type="manual_qa", path="/tmp/x",
                        product_area="general")


def make_raw(n: int) -> RawQA:
    return RawQA(native_id=str(n), title=f"t{n}", question="q", answer="a",
                 url="https://x", doc_type="manual_qa",
                 created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))


class StubConnector(Connector):
    def __init__(self, config, items=(), error=None):
        super().__init__(config)
        self.items, self.error = list(items), error
        self.seen_since = "UNSET"

    async def fetch(self, since):
        self.seen_since = since
        if self.error:
            raise self.error
        for item in self.items:
            yield item


class StubRefiner:
    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)

    async def refine(self, raw, config):
        if raw.native_id in self.fail_ids:
            return None
        from advisor_shared.documents import QADocument
        return QADocument(
            id=QADocument.make_id(config.name, raw.native_id),
            title=raw.title, content="refined", keywords=[], raw_content="r",
            url=raw.url, source=config.name, doc_type="manual_qa",
            product_area=config.product_area,
            created_at=raw.created_at, resolved_at=None,
        )


class StubWriter:
    def __init__(self):
        self.docs = []

    async def upsert(self, docs):
        self.docs.extend(docs)
        return len(docs)


async def test_happy_path_reports_and_advances_watermark(tmp_path):
    wm = WatermarkStore(tmp_path / "wm.json")
    connectors = {"a": StubConnector(make_config("a"), [make_raw(1), make_raw(2)])}
    writer = StubWriter()
    reports = await run_pipeline(
        [make_config("a")], lambda c: connectors[c.name],
        StubRefiner(), writer, wm, run_started_at=NOW)
    assert reports[0].fetched == 2 and reports[0].upserted == 2
    assert reports[0].error is None
    assert len(writer.docs) == 2
    assert wm.get("a") == NOW


async def test_failing_source_isolated_and_watermark_frozen(tmp_path):
    wm = WatermarkStore(tmp_path / "wm.json")
    connectors = {
        "bad": StubConnector(make_config("bad"), error=RuntimeError("api down")),
        "good": StubConnector(make_config("good"), [make_raw(1)]),
    }
    reports = await run_pipeline(
        [make_config("bad"), make_config("good")],
        lambda c: connectors[c.name], StubRefiner(), StubWriter(), wm,
        run_started_at=NOW)
    by_name = {r.source: r for r in reports}
    assert "api down" in by_name["bad"].error
    assert by_name["good"].error is None and by_name["good"].upserted == 1
    assert wm.get("bad") is None       # 失败源水位不推进
    assert wm.get("good") == NOW


async def test_refine_failure_counted_as_skipped(tmp_path):
    wm = WatermarkStore(tmp_path / "wm.json")
    connectors = {"a": StubConnector(make_config("a"), [make_raw(1), make_raw(2)])}
    reports = await run_pipeline(
        [make_config("a")], lambda c: connectors[c.name],
        StubRefiner(fail_ids={"1"}), StubWriter(), wm, run_started_at=NOW)
    assert reports[0].skipped == 1 and reports[0].upserted == 1


async def test_full_refresh_passes_none_since(tmp_path):
    wm = WatermarkStore(tmp_path / "wm.json")
    wm.set("a", datetime(2026, 5, 1, tzinfo=timezone.utc))
    stub = StubConnector(make_config("a"), [])
    await run_pipeline([make_config("a")], lambda c: stub, StubRefiner(),
                       StubWriter(), wm, run_started_at=NOW, full_refresh=True)
    assert stub.seen_since is None
