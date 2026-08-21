"""静态升级配置表:channel → CSAM/CSA(spec 9.1)。"""
from pathlib import Path

import yaml
from pydantic import BaseModel


class Contact(BaseModel):
    role: str
    name: str
    email: str
    teams_user_id: str | None = None
    in_channel: bool = False


class _ChannelEntry(BaseModel):
    channel_id: str
    tenant: str = ""
    enterprise_slug: str | None = None
    github_org: str | None = None
    org_token_env: str | None = None
    contacts: list[Contact]


class EscalationConfig(BaseModel):
    default_contacts: list[Contact]
    support_ticket_url: str
    channels: dict[str, _ChannelEntry]

    @classmethod
    def load(cls, path: Path) -> "EscalationConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        defaults = raw.get("defaults", {})
        entries = [_ChannelEntry(**c) for c in raw.get("channels", [])]
        return cls(
            default_contacts=[Contact(**c)
                              for c in defaults.get("contacts", [])],
            support_ticket_url=defaults.get(
                "support_ticket_url", "https://support.github.com/"),
            channels={e.channel_id: e for e in entries},
        )

    def lookup(self, channel_id: str) -> tuple[list[Contact], str]:
        entry = self.channels.get(channel_id)
        contacts = entry.contacts if entry else self.default_contacts
        return contacts, self.support_ticket_url

    def channel_entry(self, channel_id: str) -> _ChannelEntry | None:
        return self.channels.get(channel_id)
