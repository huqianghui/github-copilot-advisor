from pathlib import Path

from advisor_agent.escalation import EscalationConfig

YAML = """
defaults:
  support_ticket_url: https://support.github.com/
  contacts:
    - role: CSA
      name: 默认CSA
      email: csa@example.com

channels:
  - channel_id: "19:abc@thread.tacv2"
    tenant: 客户A
    contacts:
      - role: CSAM
        name: 李四
        email: lisi@example.com
        teams_user_id: "29:1a2b"
        in_channel: true
"""


def make_config(tmp_path: Path) -> EscalationConfig:
    p = tmp_path / "escalation.yaml"
    p.write_text(YAML, encoding="utf-8")
    return EscalationConfig.load(p)


def test_lookup_known_channel(tmp_path):
    contacts, url = make_config(tmp_path).lookup("19:abc@thread.tacv2")
    assert contacts[0].name == "李四"
    assert contacts[0].in_channel is True
    assert url == "https://support.github.com/"


def test_lookup_unknown_channel_falls_back_to_defaults(tmp_path):
    contacts, url = make_config(tmp_path).lookup("19:zzz@thread.tacv2")
    assert contacts[0].role == "CSA"
    assert contacts[0].teams_user_id is None
