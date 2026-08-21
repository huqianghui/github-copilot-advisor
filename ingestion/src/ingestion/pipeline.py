"""管道编排:每源独立执行、独立失败;摘要报告(spec 6.6、10.1)。"""
import logging
from datetime import datetime

from pydantic import BaseModel

from ingestion.config import SourceConfig

logger = logging.getLogger(__name__)

_UPSERT_BATCH = 50


class SourceReport(BaseModel):
    source: str
    fetched: int = 0
    refined: int = 0
    upserted: int = 0
    skipped: int = 0
    error: str | None = None


async def run_pipeline(sources: list[SourceConfig], connector_factory,
                       refiner, writer, watermarks,
                       run_started_at: datetime,
                       full_refresh: bool = False) -> list[SourceReport]:
    reports = []
    for config in sources:
        report = SourceReport(source=config.name)
        reports.append(report)
        try:
            since = None if full_refresh else watermarks.get(config.name)
            connector = connector_factory(config)
            batch = []
            async for raw in connector.fetch(since):
                report.fetched += 1
                doc = await refiner.refine(raw, config)
                if doc is None:
                    report.skipped += 1
                    continue
                report.refined += 1
                batch.append(doc)
                if len(batch) >= _UPSERT_BATCH:
                    report.upserted += await writer.upsert(batch)
                    batch = []
            report.upserted += await writer.upsert(batch)
            watermarks.set(config.name, run_started_at)
            logger.info("source %s done: %s", config.name, report)
        except Exception as e:
            logger.exception("source %s failed", config.name)
            report.error = str(e)
    return reports
