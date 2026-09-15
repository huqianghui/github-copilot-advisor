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
from advisor_shared.telemetry import current_span, step, timed

logger = logging.getLogger(__name__)

TEAMS_CHANNEL_ID = "msteams"
# 精确 host 相等比较。不用后缀匹配:trafficmanager.net 是共享命名空间。
ALLOWED_HOSTS = frozenset({"smba.trafficmanager.net", "api.botframework.com"})
# Azure OpenAI vision 只接受这四种。放行其他格式会让请求在 API 层报 400,
# 触发剥图降级,用户收到与真实原因无关的提示。注意:动图 GIF 同样不被接受,
# 但 content-type 分辨不出动静,只能靠 maf_backend 的不归因降级文案兜底。
SUPPORTED_IMAGE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/webp", "image/gif"})
# Teams 对内联图片下发的 contentType 是**字面通配符** image/*,不是具体 MIME
# 类型;真实格式藏在同时下发的 text/html 附件的 itemscope 里,Teams 自己在
# 下载前也不知道。官方文档(bots-filesv4)因此要求用子串匹配而非相等判断。
# 早先这里用精确白名单,于是每一张内联图片都被 select_images 丢掉、
# 而 bot.py 的 _raw_image_count(startswith)照样数到,用户永远收到
# "另有 N 张图片未能获取" —— 功能在生产环境从未工作过。
TEAMS_WILDCARD_TYPE = "image/*"
# 魔术字节。声明类型现在可能是无信息量的 image/*,响应头也可能缺失或不可信,
# 所以真实格式最终由字节自己说了算。这四条签名对各自格式是穷尽的:
# PNG 固定 8 字节签名,JPEG 固定 SOI+标记,GIF 只有 87a/89a 两个版本,
# WebP 是 RIFF 容器(size 字段占中间 4 字节,故 WEBP 在偏移 8)。
IMAGE_MAGIC_BYTES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 10.0


def _log_rejected_type(content_type: str) -> None:
    """按"这次丢弃用户看不看得见"分级,而不是一律 WARNING。

    Teams 每条带图消息都会附一个 text/html 卡片,那是常态,拒它不值得告警 ——
    一律 WARNING 会让真正的信号淹没在每条消息都有的噪声里。
    判据刻意与 bot.py 的 _raw_image_count 同口径(裸 startswith("image/")):
    它数进去的每一张,丢弃后都会变成用户看得见的"另有 N 张图片未能获取",
    那才是需要在日志里留痕的事。本次 image/* 事故正落在 WARNING 一侧。
    """
    if content_type.startswith("image/"):
        logger.warning("skipping image attachment, content type not accepted: "
                       "%s", content_type)
    else:
        logger.debug("skipping non-image attachment, content type: %s",
                     content_type or "<missing>")


def select_images(attachments) -> list[tuple[str, str]]:
    """挑出可下载的 inline image,返回 (url, content_type)。纯函数,无 I/O。

    带出 content_type **只用于日志**,绝不能拿它当 _fetch 的类型兜底:
    Teams 内联图片的声明类型是 image/*,而 ImageInput 的 ^image/[\\w.+-]+$
    不认 *(实测 ValidationError),而 bot.py 的 _to_image_inputs 在 try 之外
    调用 —— 兜回声明类型会把整个 turn 打死,比丢图严重得多。
    真实格式由 _fetch 从字节里解析,见 _resolve_image_type。
    """
    selected: list[tuple[str, str]] = []
    for attachment in attachments or []:
        content_type = (getattr(attachment, "content_type", None) or "").lower()
        # 通配符必须放行:Teams 的内联图片全都长这样(见 TEAMS_WILDCARD_TYPE)。
        # 放行不等于取消格式检查,只是把它推迟到 _fetch 拿到真实字节之后 ——
        # 那时才知道是不是 PNG/JPEG/WebP/GIF。
        # 声明了具体格式的照旧在这里判:image/svg+xml、image/bmp、image/tiff
        # 已经自报是什么,不必等下载,省一次出网。
        if (content_type != TEAMS_WILDCARD_TYPE
                and content_type not in SUPPORTED_IMAGE_TYPES):
            # 这条日志是本次事故的直接教训:精确白名单丢掉了每一张内联图片,
            # 而整段生产日志里没有一行来自本模块,排障时完全看不出原因。
            _log_rejected_type(content_type)
            continue
        url = getattr(attachment, "content_url", None)
        # 这行不只是省一次 urlparse:本函数没有 try/except,而 spec §5.3 要求
        # 图片处理永不抛异常。urlparse(None) 不报错,返回 hostname=None 的
        # ParseResultBytes,眼下靠下游 scheme 检查短路挡住;将来若有人把 host
        # 判断改成 parsed.hostname.endswith(...) 之类先解引用的写法,None 会
        # 变成 AttributeError 并连累整个 turn。测试覆盖不到这条(去掉它现有
        # 用例依然全绿),所以别当它冗余删掉。
        if not url:
            # 无条件 WARNING:能走到这里说明它已经过了类型闸,是一张
            # _raw_image_count 会数进去的图,丢掉必然对用户可见。
            logger.warning("skipping attachment with no content url; "
                           "content_type=%s", content_type)
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


def _sniff_image_type(content: bytes) -> str | None:
    """从魔术字节判定真实格式,判不出返回 None。"""
    for signature, image_type in IMAGE_MAGIC_BYTES:
        if content.startswith(signature):
            return image_type
    # WebP 是 RIFF 容器:中间 4 字节是长度字段,不能整体 startswith。
    # 切片对短 bytes 安全(返回短切片,比不上就是 False),不会 IndexError。
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _resolve_image_type(response_type: str, content: bytes) -> str | None:
    """定出可以交给 Azure OpenAI 的具体类型;定不出返回 None(该图作废)。

    返回值恒为 SUPPORTED_IMAGE_TYPES 的成员或 None —— 格式检查并没有因为
    select_images 放行 image/* 而消失,只是挪到了这里:此刻才有真实字节。
    绝不返回 image/*,也绝不在判不出时猜一个(猜错会把 GIF 谎报成 PNG,
    或者把 SVG 之类当图片发出去,在 API 层报 400 后触发无关的剥图降级)。
    """
    if response_type in SUPPORTED_IMAGE_TYPES:
        return response_type
    return _sniff_image_type(content)


class TeamsImageDownloader(InputFileDownloader):
    """下载 Teams inline image。由 AgentApplication 的 file_downloaders 管线调用,
    结果写入 state.temp.input_files。"""

    def __init__(self, connection_manager):
        self._connection_manager = connection_manager

    async def download_files(self, context: TurnContext) -> list[InputFile]:
        """SDK 不保护这个方法:agent_application.py 的 _handle_file_downloads 是
        裸 `await file_downloader.download_files(context)`,而外层 _on_turn 只
        `except ApplicationError` —— ValueError / AttributeError / httpx.* 都会
        穿透到 aiohttp。后果不是"这张图没下到",而是整个 turn 死掉:没有回复、
        没有 FALLBACK_MESSAGE、没有 typing 指示器,连 turn_state.save() 都被跳过。

        本文件开头声称"图片处理永不抛异常"(spec §5.3),那句话此前只由
        select_images 里对 url 为空的短路守着,守不住格式错误的字符串:
        Attachment 对 content_url 不做任何校验,而 urlparse('https://[abc')
        抛 ValueError('Invalid IPv6 URL')(两者均已实测)。这层 except 就是
        那句不变式的实现,不是可有可无的防御性代码。
        """
        try:
            return await self._download_all(context)
        except Exception as error:
            # 兜底,不是替代:_access_token 那处的内层 except 保留着,
            # 它给的是"取 token 失败"这条精确日志,这里只知道"某处炸了"。
            logger.error("image download failed, continuing without images error_type=%s",
                         type(error).__name__)
            return []

    @timed("teams.downloads")
    async def _download_all(self, context: TurnContext) -> list[InputFile]:
        if context.activity.channel_id != TEAMS_CHANNEL_ID:
            return []
        images = select_images(context.activity.attachments)
        timing = current_span()
        if timing is not None:
            timing.attributes["image_count"] = len(images)
        if not images:
            return []                     # 无图不取 token
        try:
            token = await self._access_token(context)
        except Exception as error:
            if timing is not None:
                timing.status = "degraded"
            logger.error("failed to acquire bot token for attachments error_type=%s",
                         type(error).__name__)
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
        if timing is not None:
            timing.attributes["result_count"] = len(files)
            if len(files) < len(images):
                timing.status = "degraded"
        return files

    @timed("teams.download_token")
    async def _access_token(self, context: TurnContext) -> str:
        provider = self._connection_manager.get_token_provider_from_activity(
            context.identity, context.activity)
        return await provider.get_access_token(
            context.identity.get_token_audience(),
            context.identity.get_token_scope())

    async def _fetch(self, client: httpx.AsyncClient, url: str,
                     declared_type: str, token: str) -> InputFile | None:
        try:
            with step("teams.download_request") as timing:
                response = await client.get(
                    url, headers={"Authorization": f"Bearer {token}"})
                timing.attributes["http_status"] = response.status_code
                timing.attributes["size_bytes"] = len(response.content)
                if response.status_code != 200:
                    timing.status = "degraded"
        except Exception as error:
            logger.warning("attachment download failed error_type=%s",
                           type(error).__name__)
            return None

        if response.status_code != 200:
            # 绝不在 401/403 后重试加 token(GHSA-7vwx-582j-j332)
            logger.warning("attachment download status=%s", response.status_code)
            return None

        content = response.content
        # 空 body 的 200 也要挡:放过去会一路走到 "data:image/png;base64,"
        # 然后由 Azure OpenAI 报 400,白烧一次往返还触发剥图降级。
        # 空内容与过大分别记录,使用 trace_id 关联,不记录可能带签名的 URL。
        if not content:
            logger.warning("attachment has empty body, skipped")
            return None
        if len(content) > MAX_IMAGE_BYTES:
            logger.warning("attachment too large (%d bytes), skipped", len(content))
            return None

        response_type = (response.headers.get("content-type", "")
                         .split(";")[0].strip().lower())
        content_type = _resolve_image_type(response_type, content)
        if content_type is None:
            # 拒绝,不猜。declared_type 在这里**只进日志**:Teams 内联图片的
            # 声明类型是 image/*,拿它兜底会让 bot.py 的 _to_image_inputs 在
            # 构造 ImageInput 时抛 ValidationError(pattern 不认 *),而那处
            # 调用在 try 之外 —— 整个 turn 死掉,用户一个字都收不到。
            logger.warning(
                "attachment format not recognized, skipped; declared=%s "
                "response=%s",
                declared_type, response_type or "<missing>")
            return None
        return InputFile(content=content, content_type=content_type,
                         content_url=url)
