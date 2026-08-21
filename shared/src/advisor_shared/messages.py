"""渠道适配层与 agent core 的唯一消息契约(spec 8.1)。"""
from pydantic import BaseModel


class Citation(BaseModel):
    title: str
    url: str


class MentionDirective(BaseModel):
    """agent 输出的结构化 @建议;mention entity 由各渠道 adapter 拼装。"""
    name: str
    platform_user_id: str
    role: str


class AdvisorRequest(BaseModel):
    text: str
    conversation_key: str
    channel_id: str
    user_id: str
    user_name: str
    is_group: bool


class AdvisorResponse(BaseModel):
    markdown: str
    citations: list[Citation] = []
    mentions: list[MentionDirective] = []
