# channels/teams/src/teams_adapter/downloader.py
"""Teams inline image 下载器(图片输入 spec §5)。

安全约束(GHSA-7vwx-582j-j332 —— Teams 附件下载器泄漏 bot bearer token):
  1. 精确 host 白名单,host 不在其中一律跳过,不发起任何请求
     —— 由本文件的 select_images 落实,已生效
  2. 不跟随重定向 —— 跟随会把 Authorization 带到重定向目标
     —— 尚未实现,由 Task 7 的 _fetch 落实
  3. 401/403 后绝不自动重试加 token —— 这正是该漏洞的成因
     —— 尚未实现,由 Task 7 的 _fetch 落实
本文件目前只有第 1 条:它是纯函数,不发起任何网络请求。
本模块刻意不支持 file upload 附件(SharePoint downloadUrl),见 spec §12。
"""
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TEAMS_CHANNEL_ID = "msteams"
# 精确 host 相等比较。不用后缀匹配:trafficmanager.net 是共享命名空间。
ALLOWED_HOSTS = frozenset({"smba.trafficmanager.net", "api.botframework.com"})
# Azure OpenAI vision 只接受这四种。放行其他格式会让请求在 API 层报 400,
# 触发剥图降级,用户收到与真实原因无关的提示。注意:动图 GIF 同样不被接受,
# 但 content-type 分辨不出动静,只能靠 maf_backend 的不归因降级文案兜底。
SUPPORTED_IMAGE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/webp", "image/gif"})
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 10.0


def select_images(attachments) -> list[tuple[str, str]]:
    """挑出可下载的 inline image,返回 (url, content_type)。纯函数,无 I/O。

    带出 content_type 是给 _fetch 兜底用的:响应头缺失或不可信时,
    用这里已经校验过的声明类型,而不是无脑塞 image/png。
    """
    selected: list[tuple[str, str]] = []
    for attachment in attachments or []:
        content_type = (getattr(attachment, "content_type", None) or "").lower()
        # 不在白名单即丢弃,顺带排除了 Teams 附带的 text/html
        if content_type not in SUPPORTED_IMAGE_TYPES:
            continue
        url = getattr(attachment, "content_url", None)
        # 这行不只是省一次 urlparse:本函数没有 try/except,而 spec §5.3 要求
        # 图片处理永不抛异常。urlparse(None) 不报错,返回 hostname=None 的
        # ParseResultBytes,眼下靠下游 scheme 检查短路挡住;将来若有人把 host
        # 判断改成 parsed.hostname.endswith(...) 之类先解引用的写法,None 会
        # 变成 AttributeError 并连累整个 turn。测试覆盖不到这条(去掉它现有
        # 用例依然全绿),所以别当它冗余删掉。
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            logger.warning(
                "skipping attachment from non-allowlisted host: %s",
                parsed.hostname)
            continue
        selected.append((url, content_type))
        if len(selected) >= MAX_IMAGES:
            break
    return selected
