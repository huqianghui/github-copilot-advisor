"""本地人工整理问答目录:一文件一 QA,frontmatter + Question/Answer 两节。"""
import re
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from ingestion.connectors.base import Connector, RawQA

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_SECTION = re.compile(r"^## (Question|Answer)\s*\n(.*?)(?=^## |\Z)", re.DOTALL | re.MULTILINE)


def _to_utc(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    raise ValueError(f"invalid created_at: {value!r}")


class ManualQAConnector(Connector):
    async def fetch(self, since: datetime | None) -> AsyncIterator[RawQA]:
        for path in sorted(Path(self.config.path).glob("*.md")):
            qa = self._parse(path)
            if qa is None:
                continue
            if since and qa.created_at <= since:
                continue
            yield qa

    def _parse(self, path: Path) -> RawQA | None:
        m = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
        if not m:
            return None
        meta = yaml.safe_load(m.group(1)) or {}
        sections = {name: body.strip() for name, body in _SECTION.findall(m.group(2))}
        if "Question" not in sections or "Answer" not in sections:
            return None
        created = _to_utc(meta.get("created_at"))
        return RawQA(
            native_id=path.name,
            title=meta.get("title", path.stem),
            question=sections["Question"],
            answer=sections["Answer"],
            url=meta.get("url", ""),
            labels=list(meta.get("labels", [])),
            doc_type="manual_qa",
            created_at=created,
            resolved_at=created,
        )
