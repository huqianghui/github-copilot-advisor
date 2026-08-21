"""每源 since 水位:上次成功同步时间,JSON 落盘(spec 6.5)。"""
import json
from datetime import datetime
from pathlib import Path


class WatermarkStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, str] = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, source_name: str) -> datetime | None:
        raw = self._data.get(source_name)
        return datetime.fromisoformat(raw) if raw else None

    def set(self, source_name: str, value: datetime) -> None:
        self._data[source_name] = value.isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.rename(self.path)
