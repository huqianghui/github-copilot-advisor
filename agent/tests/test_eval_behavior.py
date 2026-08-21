"""行为回归评估:需真实 Azure OpenAI + AI Search(灌过数据)。
prompt/工具描述每次改动必跑:uv run pytest -m integration agent/tests/test_eval_behavior.py"""
import os
import re
from pathlib import Path

import pytest
import yaml

from advisor_shared.events import AdvisorEvent
from advisor_shared.messages import AdvisorRequest

pytestmark = pytest.mark.integration

REQUIRED_ENV = ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY",
                "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY"]

CASES = yaml.safe_load(
    (Path(__file__).parent / "eval_cases.yaml").read_text(encoding="utf-8")
)["cases"]


@pytest.fixture(autouse=True)
def require_env():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        pytest.skip(f"missing env: {missing}")


def is_mostly_chinese(text: str) -> bool:
    han = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[a-zA-Z]", text))
    return han > latin * 0.5


def make_request(text: str) -> AdvisorRequest:
    return AdvisorRequest(text=text, conversation_key=f"eval-{hash(text)}",
                          channel_id="19:eval", user_id="u",
                          user_name="eval", is_group=True)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
async def test_eval_case(case):
    from advisor_agent.factory import build_advisor, set_current_channel_id
    events: list[AdvisorEvent] = []
    core = build_advisor(channel_name="eval")
    core.event_sink = events.append
    set_current_channel_id("19:eval")

    turns = case.get("multi_turn") or [case["text"]]
    key = f"eval-{case['id']}"
    for text in turns:
        req = make_request(text)
        req = req.model_copy(update={"conversation_key": key})
        resp = await core.handle(req)

    assert events[-1].stage in case["expected_stage_in"], \
        f"stage={events[-1].stage}, want {case['expected_stage_in']}"
    if case.get("expect_mention"):
        assert resp.mentions or "@" in resp.markdown or events[-1].mentioned_human
    if case.get("reply_language") == "zh":
        assert is_mostly_chinese(resp.markdown), resp.markdown[:200]
    elif case.get("reply_language") == "en":
        assert not is_mostly_chinese(resp.markdown), resp.markdown[:200]
