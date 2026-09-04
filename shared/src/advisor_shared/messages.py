"""渠道适配层与 agent core 的唯一消息契约(spec 8.1)。"""
from pydantic import BaseModel, Field


class ImageInput(BaseModel):
    """用户发送的图片。存原始字节,base64 编码是 backend 的实现细节。

    注意:含真实二进制的实例不能走 JSON 序列化 —— pydantic 默认的 bytes
    序列化器会尝试 UTF-8 解码,非 UTF-8 字节(真实 PNG)抛
    PydanticSerializationError,而恰好合法 UTF-8 的字节会静默变成乱码字符串
    (测试夹具用短 ASCII 时看不出问题)。当前 AdvisorRequest 全程进程内传递,
    从不序列化。若日后需要跨进程/持久化,须同时加 field_serializer(base64)
    与配套 validator,否则往返会静默损坏图片。
    """
    data: bytes = Field(repr=False)
    mime_type: str = Field(pattern=r"^image/[\w.+-]+$")
    name: str = ""


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
    images: list[ImageInput] = []


class AdvisorResponse(BaseModel):
    markdown: str
    citations: list[Citation] = []
    mentions: list[MentionDirective] = []
