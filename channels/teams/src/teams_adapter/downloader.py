# channels/teams/src/teams_adapter/downloader.py
"""Teams inline image 下载器(图片输入 spec §5)。

安全约束(GHSA-7vwx-582j-j332 —— Teams 附件下载器泄漏 bot bearer token):
  1. 精确 host 白名单,host 不在其中一律跳过,不发起任何请求
     —— 由本文件的 select_images 落实,已生效
  2. 不跟随重定向 —— 跟随会把 Authorization 带到重定向目标
     —— 由 TeamsImageDownloader 的 follow_redirects=False 落实,已生效
  3. 401/403 后绝不自动重试加 token —— 这正是该漏洞的成因
     —— 由 _fetch 落实(非 200 一律放弃,不重试),已生效
select_images 是纯函数,不发起任何网络请求;唯一出网的地方是 _fetch。
本模块刻意不支持 file upload 附件(SharePoint downloadUrl),见 spec §12。
"""
import logging
from urllib.parse import urlparse

import httpx
from microsoft_agents.hosting.core import TurnContext
from microsoft_agents.hosting.core.app.input_file import (
    InputFile,
    InputFileDownloader,
)

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
        # userinfo 必须拒绝,而不只是查 hostname:白名单查的是 hostname,
        # httpx 消费的是 netloc,userinfo 正好夹在两者之间,且 geturl()
        # 不会把它去掉 —— 所以下面那行 geturl() 也归一化不掉这条轴。
        # 放行 https://attacker:secret@smba.trafficmanager.net/... 的后果:
        # httpx 会拿 userinfo 造 BasicAuth 顶掉我们的 Authorization
        # (实测发出的是 Basic YXR0YWNrZXI6c2VjcmV0),于是攻击者可以
        # 确定性地让任意图片下载失败(图片压制原语);同时这段
        # 攻击者可控的 user:pass 会原样进 WARNING 日志和 InputFile.content_url。
        if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
                or parsed.username or parsed.password):
            # 只打 hostname,绝不打原始 URL:userinfo 分支里的 user:pass
            # 是攻击者可控的,原样进日志等于把它抄进我们的运维系统。
            # 也因此这条文案不能说死"host 不在白名单" —— userinfo 那条的
            # hostname 恰恰是白名单内的,那样写会把排障引到错误方向。
            logger.warning(
                "skipping attachment, url rejected by scheme/host/userinfo "
                "checks; host=%s", parsed.hostname)
            continue
        selected.append((url, content_type))
        if len(selected) >= MAX_IMAGES:
            break
    return selected


class TeamsImageDownloader(InputFileDownloader):
    """下载 Teams inline image。由 AgentApplication 的 file_downloaders 管线调用,
    结果写入 state.temp.input_files。"""

    def __init__(self, connection_manager):
        self._connection_manager = connection_manager

    async def download_files(self, context: TurnContext) -> list[InputFile]:
        if context.activity.channel_id != TEAMS_CHANNEL_ID:
            return []
        images = select_images(context.activity.attachments)
        if not images:
            return []                     # 无图不取 token
        try:
            token = await self._access_token(context)
        except Exception:
            logger.exception("failed to acquire bot token for attachments")
            return []

        files: list[InputFile] = []
        # follow_redirects=False:跟随会把 Authorization 带到重定向目标
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_S,
                                     follow_redirects=False) as client:
            for url, declared_type in images:
                # 消除校验器/取用器差分:select_images 校验的是 urlparse(url)
                # 的结果,而 httpx/yarl 会对原始字符串**重新解析**。两个解析器
                # 对同一字符串理解不同时,校验就形同虚设。实测两类输入存在差分
                # (URL 内嵌 CRLF、前导空白),今日均 fail-closed,但那是依赖库
                # 当前行为带来的运气,不是设计保证。
                downloaded = await self._fetch(client, urlparse(url).geturl(),
                                               declared_type, token)
                if downloaded is not None:
                    files.append(downloaded)
        return files

    async def _access_token(self, context: TurnContext) -> str:
        provider = self._connection_manager.get_token_provider_from_activity(
            context.identity, context.activity)
        return await provider.get_access_token(
            context.identity.get_token_audience(),
            context.identity.get_token_scope())

    async def _fetch(self, client: httpx.AsyncClient, url: str,
                     declared_type: str, token: str) -> InputFile | None:
        try:
            response = await client.get(
                url, headers={"Authorization": f"Bearer {token}"})
        except Exception:
            logger.warning("attachment download failed: %s", url, exc_info=True)
            return None

        if response.status_code != 200:
            # 绝不在 401/403 后重试加 token(GHSA-7vwx-582j-j332)
            logger.warning("attachment download status=%s url=%s",
                           response.status_code, url)
            return None

        content = response.content
        # 空 body 的 200 也要挡:放过去会一路走到 "data:image/png;base64,"
        # 然后由 Azure OpenAI 报 400,白烧一次往返还触发剥图降级。
        # 两种情况分开打日志:合并成一条会打出 "size rejected (0 bytes)"
        # 这种自相矛盾的文案,且都缺 URL,与上面的状态码告警关联不起来。
        if not content:
            logger.warning("attachment has empty body, skipped: %s", url)
            return None
        if len(content) > MAX_IMAGE_BYTES:
            logger.warning("attachment too large (%d bytes), skipped: %s",
                           len(content), url)
            return None

        content_type = (response.headers.get("content-type", "")
                        .split(";")[0].strip().lower())
        if content_type not in SUPPORTED_IMAGE_TYPES:
            # 响应头不可信时退回 select_images 已校验过的声明类型,
            # 而不是无脑塞 image/png —— 那会把 GIF 谎报成 PNG。
            content_type = declared_type
        return InputFile(content=content, content_type=content_type,
                         content_url=url)
