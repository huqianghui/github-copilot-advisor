import pytest

from test_eval_behavior import assert_concise_answer


ANSWER = (
    "\u73b0\u8c61\u603b\u7ed3\u3002\u53ef\u80fd\u539f\u56e0\u3002"
    "\u5c1a\u672a\u786e\u8ba4\u3002\n\n"
    "\u5efa\u8bae\u5c1d\u8bd5:\n1. A\n2. B\n3. C\n\n"
    "\u6765\u6e90:\nhttps://example.com/a"
)


def test_eval_accepts_short_three_part_answer():
    assert_concise_answer(ANSWER)


def test_eval_accepts_two_sentence_summary():
    assert_concise_answer(ANSWER.replace("\u5c1a\u672a\u786e\u8ba4\u3002", ""))


@pytest.mark.parametrize("answer", [
    ANSWER.replace("\u53ef\u80fd\u539f\u56e0\u3002\u5c1a\u672a\u786e\u8ba4\u3002", ""),
    "\u8865\u5145\u3002" + ANSWER,
    ANSWER.replace("3. C", "C"),
    ANSWER.replace("3. C", "3. " + "x" * 600),
    ANSWER + "\nhttps://example.com/b\nhttps://example.com/c\nhttps://example.com/d",
    ANSWER + "\nhttps://example.com/a",
    ANSWER + "\n[Duplicate](https://example.com/a)",
])
def test_eval_rejects_length_structure_and_source_regressions(answer):
    with pytest.raises(AssertionError):
        assert_concise_answer(answer)
