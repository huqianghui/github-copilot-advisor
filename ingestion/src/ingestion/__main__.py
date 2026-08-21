"""CLI:python -m ingestion run [--source NAME] [--full-refresh]。"""
import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from azure.core.credentials import AzureKeyCredential
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from openai import AsyncAzureOpenAI

from ingestion import connectors
from ingestion.config import load_sources
from ingestion.pipeline import run_pipeline
from ingestion.refine import Refiner
from ingestion.search_writer import SearchWriter, ensure_index
from ingestion.watermark import WatermarkStore

INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX", "copilot-qa")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ingestion")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--source", help="只跑指定源")
    run.add_argument("--full-refresh", action="store_true")
    run.add_argument("--sources-file", type=Path,
                     default=Path(__file__).parent.parent.parent / "sources.yaml")
    return parser


async def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args()

    sources = load_sources(args.sources_file)
    if args.source:
        sources = [s for s in sources if s.name == args.source]
        if not sources:
            print(f"unknown source: {args.source}", file=sys.stderr)
            return 2

    endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
    key = AzureKeyCredential(os.environ["AZURE_SEARCH_API_KEY"])
    ensure_index(SearchIndexClient(endpoint, key), INDEX_NAME)

    openai_client = AsyncAzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version="2024-10-21",
    )
    search_client = SearchClient(endpoint, INDEX_NAME, key)
    try:
        reports = await run_pipeline(
            sources, connectors.create,
            Refiner(openai_client), SearchWriter(search_client, openai_client),
            WatermarkStore(Path(".state/watermarks.json")),
            run_started_at=datetime.now(timezone.utc),
            full_refresh=args.full_refresh,
        )
    finally:
        await search_client.close()
        await openai_client.close()

    failed = False
    for r in reports:
        status = f"ERROR: {r.error}" if r.error else "ok"
        print(f"{r.source}: fetched={r.fetched} refined={r.refined} "
              f"upserted={r.upserted} skipped={r.skipped} [{status}]")
        failed = failed or bool(r.error)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
