# channels/teams/src/teams_adapter/render.py
"""AdvisorResponse → Teams activity dict:mention entity 在此拼装(spec 8.1/8.2)。"""
from advisor_shared.messages import AdvisorResponse

MAX_MARKDOWN_CHARS = 6000
_TRUNCATION_NOTE = "\n\n(内容过长已截断,完整信息见引用链接)"


def render_reply(response: AdvisorResponse) -> dict:
    text = response.markdown
    if len(text) > MAX_MARKDOWN_CHARS:
        text = text[:MAX_MARKDOWN_CHARS] + _TRUNCATION_NOTE

    entities = []
    if response.mentions:
        at_tags = []
        for m in response.mentions:
            tag = f"<at>{m.name}</at>"
            at_tags.append(tag)
            entities.append({
                "type": "mention", "text": tag,
                "mentioned": {"id": m.platform_user_id, "name": m.name},
            })
        text = " ".join(at_tags) + " " + text

    if response.citations:
        refs = "\n".join(f"- [{c.title}]({c.url})" for c in response.citations)
        text = f"{text}\n\n**参考:**\n{refs}"

    return {"type": "message", "text": text, "entities": entities}
