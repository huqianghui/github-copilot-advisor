"""LLM 提炼:RawQA → QADocument。content 只存提炼结果,原文只做召回(spec 6.2)。"""
import logging
import re

from advisor_shared.documents import QADocument, RAW_CONTENT_MAX_CHARS
from ingestion.config import SourceConfig
from ingestion.connectors.base import RawQA

logger = logging.getLogger(__name__)

THEME_TAGS = [
    "billing-credits", "stability-network", "agent-routing", "ide-compat",
    "context-session", "mcp-integration", "usage-visibility", "m365-workiq",
    "platform-limits", "advanced-usecases",
]

_THEME_TAG_LINE_RE = re.compile(r"^\s*主题标签[:：]\s*(.+?)\s*$", re.MULTILINE)


def build_refine_prompt(raw: RawQA) -> str:
    tag_list = "、".join(THEME_TAGS)
    return (
        "你是知识库编辑。把下面的问答提炼成简洁条目,用于支持机器人直接引用回答。\n"
        "要求:保留问题要点与完整解决步骤;剥离寒暄、模板、日志噪声;"
        "不超过 500 token;与原文语言保持一致。\n"
        f"此外,从以下固定清单中选择 1-2 个最贴切的主题标签:{tag_list}。"
        "在提炼内容的最后单独一行,按格式输出:主题标签: tag1, tag2\n\n"
        f"标题:{raw.title}\n\n问题:\n{raw.question}\n\n解答:\n{raw.answer}"
    )


def _extract_theme_tags(content: str) -> tuple[str, list[str]]:
    """从 content 末尾解析 `主题标签: tag1, tag2` 行,返回(剥离后的 content, 标签列表)。

    解析不到就返回原 content 与空列表,不报错。
    """
    match = _THEME_TAG_LINE_RE.search(content)
    if not match:
        return content, []
    raw_tags = match.group(1)
    tags = [t.strip() for t in re.split(r"[,,、]", raw_tags) if t.strip()]
    valid_tags = [t for t in tags if t in THEME_TAGS]
    if not valid_tags:
        return content, []
    stripped = (content[: match.start()] + content[match.end():]).strip()
    return stripped, valid_tags


class Refiner:
    def __init__(self, chat_client, model: str = "gpt-4o"):
        self.chat = chat_client
        self.model = model

    async def refine(self, raw: RawQA, config: SourceConfig) -> QADocument | None:
        try:
            resp = await self.chat.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": build_refine_prompt(raw)}],
                temperature=0.1,
            )
            content = resp.choices[0].message.content.strip()
        except Exception:
            logger.exception("refine failed for %s:%s", config.name, raw.native_id)
            return None
        content, theme_tags = _extract_theme_tags(content)
        raw_content = f"{raw.question}\n\n{raw.answer}"[:RAW_CONTENT_MAX_CHARS]
        keywords = sorted({*raw.labels, config.product_area, *theme_tags})
        return QADocument(
            id=QADocument.make_id(config.name, raw.native_id),
            title=raw.title,
            content=content,
            keywords=keywords,
            raw_content=raw_content,
            url=raw.url,
            source=config.name,
            doc_type=raw.doc_type,
            product_area=config.product_area,
            created_at=raw.created_at,
            resolved_at=raw.resolved_at,
        )
