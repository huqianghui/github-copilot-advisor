# channels/teams/tests/test_downloader.py
"""select_images:附件筛选、格式白名单与 host 白名单(图片输入 spec §5)。

只覆盖纯函数的选取逻辑;实际下载(重定向、超时、大小上限、token)属 Task 7。
"""
import httpx
import pytest
import respx
from advisor_shared.messages import ImageInput
from microsoft_agents.activity import Activity

from teams_adapter.bot import _activity_to_dict, _raw_image_count
from teams_adapter.downloader import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    TeamsImageDownloader,
    select_images,
)

GOOD_URL = "https://smba.trafficmanager.net/amer/v3/attachments/1/views/original"
EVIL_URL = "https://evil.example.com/steal"


def attachments_activity(attachments: list[dict]) -> Activity:
    return Activity.model_validate({
        "type": "message",
        "channelId": "msteams",
        "text": "看看这个",
        "conversation": {"id": "19:c", "conversationType": "channel"},
        "from": {"id": "29:u", "name": "n"},
        "attachments": attachments,
    })


def urls_for(attachments: list[dict]) -> list[str]:
    """只取 URL,便于断言;格式与 content_type 的传递另有专门用例。"""
    return [url for url, _ in
            select_images(attachments_activity(attachments).attachments)]


def test_keeps_only_image_attachments():
    # Teams 会把 text/html 与图片一并下发,不过滤会去下载 HTML
    assert urls_for([
        {"contentType": "text/html", "contentUrl": GOOD_URL},
        {"contentType": "application/pdf", "contentUrl": GOOD_URL},
        {"contentType": "image/png", "contentUrl": GOOD_URL},
    ]) == [GOOD_URL]


def test_rejects_non_allowlisted_host():
    assert urls_for([{"contentType": "image/png", "contentUrl": EVIL_URL}]) == []


def test_rejects_non_https_scheme():
    plain = GOOD_URL.replace("https://", "http://")
    assert urls_for([{"contentType": "image/png", "contentUrl": plain}]) == []


def test_rejects_lookalike_host_bypasses():
    """host 必须精确相等。trafficmanager.net 是共享命名空间,
    任何"沾边就放行"的写法都会把攻击者可注册的域名放进来。

    下面四个样本是按变异实测挑的,注释即实测结果(勿凭直觉改):
      S1 evil.trafficmanager.net
         杀 host.endswith("trafficmanager.net")、"tm.net" in host
      S2 evilsmba.trafficmanager.net —— 唯一能杀 endswith("smba.trafficmanager.net")
         的样本:它以该串结尾却缺少点边界
      S3 smba.trafficmanager.net.attacker.com —— 唯一能杀前缀匹配
         any(host.startswith(h)) 的样本。attacker.com 可被任意注册,
         这正是 GHSA-7vwx-582j-j332 泄漏 token 的形状。
         注意:前面加 "evil-" 会让它不再以 smba.trafficmanager.net 开头,
         前缀变异就跟着拒绝它,这条样本也就白写了 —— 别加前缀。
      S4 smba.trafficmanager.net@evil.com
         杀 netloc.startswith(h) 及在 netloc/整个 URL 上做子串匹配的写法
    S2、S3 各自唯一钉死一种变异,删不得;S1、S4 对上述变异集属冗余,
    保留是为了覆盖没枚举到的相邻写法,成本只有两行。
    """
    for lookalike in (
        "https://evil.trafficmanager.net/x",
        "https://evilsmba.trafficmanager.net/x",
        "https://smba.trafficmanager.net.attacker.com/x",
        "https://smba.trafficmanager.net@evil.com/x",
    ):
        assert urls_for(
            [{"contentType": "image/png", "contentUrl": lookalike}]
        ) == [], lookalike


def test_skips_attachment_without_url():
    assert urls_for([{"contentType": "image/png"}]) == []


def test_caps_image_count():
    # 输入量随 MAX_IMAGES 走:写死 10 的话,一旦有人把 MAX_IMAGES 提到 10,
    # 就变成喂 10 个断言 10 个,截断逻辑不再被覆盖且没有测试变红。
    many = ([{"contentType": "image/png", "contentUrl": GOOD_URL}]
            * (MAX_IMAGES + 3))
    assert len(urls_for(many)) == MAX_IMAGES


def test_handles_none_attachments():
    assert select_images(None) == []


def test_accepts_teams_wildcard_content_type():
    """真实 Teams 内联图片的 contentType 是**字面通配符** image/*。

    这是生产事故的直接回归:此前的精确白名单 `content_type in
    SUPPORTED_IMAGE_TYPES` 把它拒了,于是每一张内联图片都下不来,而 bot.py 的
    _raw_image_count(裸 startswith)照样数到 1 —— 用户永远只收到
    "(另有 1 张图片未能获取)",功能在生产环境从未工作过。

    整套夹具此前只喂具体类型(image/png、image/jpeg),样本里根本没有
    真实世界的那一类,所以 34 个用例全绿也判别不出这个 bug。

    官方文档(bots-filesv4)明说要子串匹配而非相等:Teams 在下发时自己也
    不知道确切格式,真实格式藏在同时下发的 text/html 附件的 itemscope 里。
    """
    assert urls_for([{"contentType": "image/*", "contentUrl": GOOD_URL}]) == [
        GOOD_URL]


def test_wildcard_acceptance_is_not_a_blanket_image_prefix_match():
    """放行 image/* 不等于退回 startswith("image/")。

    与上一条配对:只有这两条同时在,才既钉住"通配符必须收"、又钉住
    "具体的不支持格式仍要拒"。SVG 是可执行脚本格式,不该转发;BMP/TIFF
    过不了 Azure OpenAI,放行会让请求在 API 层报 400 → 触发剥图降级 ——
    用户收到"这次没能处理你发送的图片",而功能其实好好的,只是格式不对。

    这些类型**已经自报了自己是什么**,不必等下载,所以仍然在这一层拒掉,
    还省一次出网;通配符没有自报,才需要推迟到 _fetch 看真实字节。
    """
    for bad in ("image/svg+xml", "image/bmp", "image/tiff",
                "image/vnd.microsoft.icon"):
        assert urls_for([{"contentType": bad, "contentUrl": GOOD_URL}]) == [], bad


def test_returns_content_type_alongside_url():
    """content_type 要带出去,但只用于日志 —— 不是 _fetch 的类型兜底。

    兜底那条路已经拆掉了:真实内联图片的声明类型是 image/*,而 ImageInput 的
    ^image/[\\w.+-]+$ 不认 *(实测 ValidationError),bot.py 的 _to_image_inputs
    又在 try 之外调用,兜回声明类型会打死整个 turn。见
    test_never_yields_a_wildcard_content_type。
    """
    assert select_images(attachments_activity(
        [{"contentType": "image/jpeg", "contentUrl": GOOD_URL}]).attachments
    ) == [(GOOD_URL, "image/jpeg")]


def test_content_type_is_normalized_to_lowercase():
    assert select_images(attachments_activity(
        [{"contentType": "IMAGE/PNG", "contentUrl": GOOD_URL}]).attachments
    ) == [(GOOD_URL, "image/png")]


def test_every_downloadable_attachment_is_also_counted_as_a_raw_image():
    """跨接缝(Task 7 downloader ↔ Task 9 bot):select_images 愿意下载的每一张,
    _raw_image_count 都必须数进去。

    两侧口径本就不等价,而且必须不等价:_raw_image_count 要把"下载器拒了的图"
    也算成未能获取(image/svg+xml、非白名单 host 都属此列),所以它数得比
    select_images 多。真正的不变式是单向包含 —— **能下的一定被数到**。

    包含关系一旦破,skipped = raw_images - len(images) 就会算出 0:图片全灭时
    agent 照常回答,用户以为截图被看过了(谎报);纯图片消息更会因
    raw_images == 0 走 is_empty 提前返回,一个字都不回。

    唯一能违反它的输入是 image/ 前缀带大写 —— select_images 比对白名单前先
    .lower()(即上一条 test_content_type_is_normalized_to_lowercase 钉住的契约),
    而裸 startswith("image/") 不认。所以批次里必须有大写样本。

    断言刻意让两个函数**互相**对照,而不是各自跟"预期几张"这类常量比:
    后者形同虚设 —— 同时收窄两侧照样全绿。这也是为什么此处不写
    len(downloadable) == 3 之类的数字。
    """
    # 大写样本与 image/* 排在最前:MAX_IMAGES 从尾部截断,排后面会被截掉,
    # 这条用例就退化成只测小写、只测具体类型了。可下载数正好是 MAX_IMAGES,
    # 截断只会让 downloadable 变小,单向包含照样成立,不会假绿。
    content_types = [
        "IMAGE/PNG",        # 大写 —— 下载器收,裸 startswith 的计数器漏掉
        "image/*",          # 真实 Teams 内联图片的形态:两侧都必须收
        "Image/Jpeg",       # 混合大小写,同上
        "image/png",        # 小写基线:两侧本来就一致
        "image/svg+xml",    # 是图片但 Azure OpenAI 读不了 → 数得到、下不了
        "IMAGE/BMP",        # 同上,且大写:计数器漏它同样会少报
        "text/html",        # 压根不是图片 → 两侧都不该收
        "APPLICATION/PDF",
    ]
    attachments = [{"contentType": ct, "contentUrl": f"{GOOD_URL}/{i}"}
                   for i, ct in enumerate(content_types)]
    activity = attachments_activity(attachments)

    downloadable = {url for url, _ in select_images(activity.attachments)}
    # _raw_image_count 只给出一个 int,逐张问它才能还原成可与 URL 集合比对的形状
    counted = {a["contentUrl"] for a in attachments
               if _raw_image_count(_activity_to_dict(attachments_activity([a])))}

    # 上面那次逐张分解只有在 _raw_image_count 可加时才等价于整批调用;
    # 先把这一点钉住,否则下面的集合比较可能建立在错误的还原上
    assert _raw_image_count(_activity_to_dict(activity)) == len(counted)
    # 核心:接缝不变式,两个函数互相对照
    assert downloadable <= counted, downloadable - counted
    # 防退化:批次真的把大写样本喂到了 downloader 这一侧并被接受 ——
    # 否则上面的包含关系可以靠"downloadable 为空"廉价成立
    assert f"{GOOD_URL}/0" in downloadable


@pytest.mark.parametrize("host", ["smba.trafficmanager.net",
                                  "api.botframework.com"])
@pytest.mark.parametrize("content_type", ["image/png", "image/jpeg",
                                          "image/webp", "image/gif"])
def test_accepts_every_supported_type_on_every_allowed_host(host, content_type):
    """收窄白名单是 fail-closed,不会被上面任何拒绝性用例抓到 ——
    删掉 api.botframework.com 或 image/gif 全绿。这条把两张白名单钉成
    双向断言,免得将来一次"清理"静默打掉真实用户的图片。

    这里的 host 与 content_type 必须写死字面量,不能用 sorted(ALLOWED_HOSTS)
    之类从被测常量取值:那样收窄常量只会少生成几个用例,测试照样全绿,
    等于没测(实测过,删 image/gif 是 16 passed 而非变红)。
    """
    url = f"https://{host}/v3/attachments/1/views/original"
    assert select_images(attachments_activity(
        [{"contentType": content_type, "contentUrl": url}]).attachments
    ) == [(url, content_type)]


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class StubIdentity:
    def get_token_audience(self):
        return "https://api.botframework.com"

    def get_token_scope(self):
        return ["https://api.botframework.com/.default"]


class StubProvider:
    async def get_access_token(self, resource_url, scopes, force_refresh=False):
        return "TOKEN"


class StubConnectionManager:
    def get_token_provider_from_activity(self, identity, activity):
        return StubProvider()


class FakeContext:
    def __init__(self, activity):
        self.activity = activity
        self.identity = StubIdentity()


def downloader() -> TeamsImageDownloader:
    return TeamsImageDownloader(StubConnectionManager())


async def download(attachments: list[dict], channel_id="msteams"):
    activity = attachments_activity(attachments)
    activity.channel_id = channel_id
    return await downloader().download_files(FakeContext(activity))


IMAGE_ATTACHMENT = [{"contentType": "image/png", "contentUrl": GOOD_URL}]


@respx.mock
async def test_downloads_with_bearer_token():
    route = respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=PNG, headers={"content-type": "image/png"}))
    files = await download(IMAGE_ATTACHMENT)
    assert len(files) == 1
    assert files[0].content == PNG
    assert files[0].content_type == "image/png"
    assert route.calls[0].request.headers["authorization"] == "Bearer TOKEN"


@respx.mock
async def test_download_steps_are_correlated_and_do_not_log_signed_url(caplog):
    import logging
    from advisor_shared.logging import configure_logging
    from advisor_shared.telemetry import trace_scope

    configure_logging()
    url = GOOD_URL + "?sig=private-signature"
    respx.get(url).mock(return_value=httpx.Response(403))
    with caplog.at_level(logging.INFO), trace_scope() as trace:
        assert await download([{"contentType": "image/png", "contentUrl": url}]) == []
    stages = {s.name: s for s in trace.timings}
    assert {"teams.downloads", "teams.download_token",
            "teams.download_request"} <= stages.keys()
    assert stages["teams.download_request"].attributes["http_status"] == 403
    assert stages["teams.download_request"].status == "degraded"
    assert stages["teams.downloads"].status == "degraded"
    assert "private-signature" not in caplog.text


@respx.mock
async def test_never_requests_non_allowlisted_host():
    """安全回归:非白名单 host 一次请求都不能发出。"""
    route = respx.get(EVIL_URL).mock(return_value=httpx.Response(200, content=PNG))
    files = await download([{"contentType": "image/png", "contentUrl": EVIL_URL}])
    assert files == []
    assert route.called is False


@respx.mock
async def test_does_not_follow_redirects():
    """安全回归:跟随重定向会把 Authorization 带到重定向目标。

    302 带 body 是刻意的:body 为空时 files == [] 会被空 body 守卫满足,
    与状态码检查是否严格无关 —— 把 != 200 放宽成 >= 400 照样全绿。
    带上 body 之后,这条断言才真正压在状态码上:被攻陷的白名单 host
    返回一个带 body 的 3xx 不能被当成图片收下。
    """
    first = respx.get(GOOD_URL).mock(return_value=httpx.Response(
        302, headers={"location": EVIL_URL, "content-type": "image/png"},
        content=PNG))
    evil = respx.get(EVIL_URL).mock(return_value=httpx.Response(200, content=PNG))
    files = await download(IMAGE_ATTACHMENT)
    assert files == []
    assert first.called is True
    assert evil.called is False


@respx.mock
async def test_401_is_not_retried():
    """安全回归:401 后重试加 token 正是 GHSA-7vwx-582j-j332 的成因。"""
    route = respx.get(GOOD_URL).mock(return_value=httpx.Response(401))
    files = await download(IMAGE_ATTACHMENT)
    assert files == []
    assert route.call_count == 1


@respx.mock
async def test_oversized_image_skipped():
    respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=b"0" * (MAX_IMAGE_BYTES + 1),
        headers={"content-type": "image/png"}))
    assert await download(IMAGE_ATTACHMENT) == []


@respx.mock
async def test_empty_body_skipped():
    """空 body 的 200 若放过去,会变成空 data URL 并被 Azure OpenAI 拒,
    白烧一次往返还误触剥图降级。"""
    respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=b"", headers={"content-type": "image/png"}))
    assert await download(IMAGE_ATTACHMENT) == []


@respx.mock
async def test_network_error_does_not_raise():
    """图片是增强,不能成为新的失败源。"""
    respx.get(GOOD_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert await download(IMAGE_ATTACHMENT) == []


@respx.mock
async def test_rejects_userinfo_in_url():
    """带 userinfo 的 URL 一律拒绝,且一次请求都不发。

    urlparse('https://attacker:secret@smba.trafficmanager.net/...').hostname
    就是 smba.trafficmanager.net,单查 hostname 会放行它。但白名单查的是
    hostname、httpx 消费的是 netloc,userinfo 夹在两者之间,而
    geturl() **保留** userinfo,所以那次归一化也盖不住这条轴。

    放行的实际后果(已实测):httpx 拿 userinfo 造 BasicAuth 顶掉我们的头,
    发出的是 `Authorization: Basic YXR0YWNrZXI6c2VjcmV0` —— 攻击者由此获得
    一个确定性的图片压制原语(任意图片必然 401),并把自己可控的 user:pass
    写进我们的 WARNING 日志和 InputFile.content_url。
    """
    userinfo_url = ("https://attacker:secret@smba.trafficmanager.net"
                    "/amer/v3/attachments/1/views/original")
    attachment = [{"contentType": "image/png", "contentUrl": userinfo_url}]
    # httpx 归一化后会剥掉 userinfo,所以这条路由正是它会打中的目标
    route = respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=PNG, headers={"content-type": "image/png"}))
    assert urls_for(attachment) == []
    assert await download(attachment) == []
    assert route.called is False


@respx.mock
async def test_normalizes_url_before_handing_to_httpx():
    """把 select_images 校验过的字符串归一化后再交给 httpx。

    校验器/取用器差分:select_images 校验 urlparse 的结果,httpx 会对原始
    字符串重新解析。前导空白就是实测存在的一例 ——
    httpx.URL('  https://smba…').host 是 ''(请求打不出去),而
    urlparse(...).geturl() 归一化后 host 正确。少了那次 geturl(),这张
    合法图片会静默失败;更要紧的是,这条差分轴一旦没有测试钉住,
    将来一次"清理"就会把归一化删掉而全绿。
    """
    route = respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=PNG, headers={"content-type": "image/png"}))
    files = await download(
        [{"contentType": "image/png", "contentUrl": "  " + GOOD_URL}])
    assert len(files) == 1
    assert route.called is True


@respx.mock
async def test_response_content_type_wins_when_supported():
    """响应头在白名单内时优先于声明类型 —— 声明可能过时,实际字节才算数。"""
    respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=PNG, headers={"content-type": "image/jpeg"}))
    files = await download([{"contentType": "image/png", "contentUrl": GOOD_URL}])
    assert files[0].content_type == "image/jpeg"


@respx.mock
async def test_wildcard_declaration_resolved_from_response_header():
    """声明是 image/* 时,响应头里的具体类型算数。

    这是通配符能安全放行的前提:select_images 不再知道格式了,
    格式检查整体挪到了下载之后,而响应头是第一个可能的信息来源。
    """
    respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=PNG, headers={"content-type": "image/gif"}))
    files = await download([{"contentType": "image/*", "contentUrl": GOOD_URL}])
    assert files[0].content_type == "image/gif"


# 四种格式的魔术字节样本。品种必须齐全:只喂 PNG 的话,"真的嗅探"与
# "无脑 return 'image/png'" 两种实现给出完全相同的结果,测试等于没测。
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32
GIF87 = b"GIF87a" + b"0" * 32
GIF89 = b"GIF89a" + b"0" * 32
# WebP 是 RIFF 容器:中间 4 字节是长度字段,"WEBP" 落在偏移 8,
# 所以它是唯一一个不能用 startswith 判的格式。
WEBP = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"VP8 " + b"0" * 24


@pytest.mark.parametrize("content,expected", [
    (PNG, "image/png"),
    (JPEG, "image/jpeg"),
    (GIF87, "image/gif"),     # GIF 有两个版本号,都得认
    (GIF89, "image/gif"),
    (WEBP, "image/webp"),
])
@respx.mock
async def test_magic_bytes_resolve_format_when_header_unusable(content, expected):
    """声明是 image/* **且**响应头不可用时,靠字节自己认出格式。

    这是通配符路径的最后一道防线,也是最容易被"实现成假的"的一处:
    如果这里只喂 PNG,那么 `return "image/png"` 这种无脑实现照样全绿。
    五个样本覆盖四种格式(GIF 两个版本号),每个都断言各自解析正确 ——
    任何一种"猜一个常量"的写法都会被其中至少三条抓住。

    猜错的实际后果不是抽象的:把 GIF 谎报成 PNG 会让 Azure OpenAI 报 400,
    进而触发剥图降级,用户收到与真实原因无关的提示。
    """
    respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=content, headers={"content-type": "application/octet-stream"}))
    files = await download([{"contentType": "image/*", "contentUrl": GOOD_URL}])
    assert [f.content_type for f in files] == [expected]


@pytest.mark.parametrize("content", [
    # SVG:这条路径要挡的正是它。声明成 image/svg+xml 会被 select_images 拒,
    # 但伪装成 image/* + application/octet-stream 就绕过了那一层,
    # 只剩魔术字节能认出"这不是那四种格式之一"。
    b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    # RIFF 容器但不是 WebP(这是 WAVE 音频)。唯一能杀掉
    # `content[:4] == b"RIFF"` 这种漏掉偏移 8 校验的写法的样本 ——
    # 少了它,把 WebP 判据放宽成"只看 RIFF"照样全绿。
    b"RIFF" + b"\x24\x00\x00\x00" + b"WAVE" + b"fmt " + b"0" * 24,
    b"\x89PNG",                               # 截断的 PNG 签名:不足 8 字节
])
@respx.mock
async def test_unresolvable_format_is_rejected_rather_than_guessed(content):
    """响应头和魔术字节都判不出时,拒绝该图,而不是猜一个。

    猜出来的类型必然是错的:字节既然不是那四种格式之一,贴任何一个标签
    都会在 Azure OpenAI 那边报 400 —— 白烧一次往返,还触发与真实原因
    无关的剥图降级。

    截断样本还顺带钉住"嗅探不能抛":切片写法对短 bytes 安全,
    而 content[8:12] 这类若被改成 content[8] 就会 IndexError ——
    在 download_files 的兜底 except 里会被吞成静默丢图。
    """
    respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=content,
        headers={"content-type": "application/octet-stream"}))
    assert await download([{"contentType": "image/*",
                            "contentUrl": GOOD_URL}]) == []


@respx.mock
async def test_never_yields_a_wildcard_content_type():
    """_fetch 绝不能把 image/* 透传下去 —— 那会打死整个 turn。

    断言刻意落在"能不能构造出 ImageInput"上,而不是字符串比较:那才是
    下游真正的契约。ImageInput 的 ^image/[\\w.+-]+$ 不认 *(* 不在
    [\\w.+-] 里),而 bot.py 的 _to_image_inputs 在 on_message 的 try
    **之外**调用 —— 抛出的 ValidationError 会越过 fallback:没有回复、
    没有 FALLBACK_MESSAGE、连 turn_state.save() 都被跳过,用户一个字都收不到。
    比丢一张图严重得多,而且从用户视角完全静默。

    响应头这里刻意也回 image/*(Teams 完全可能原样回显声明类型):
    这一条能杀掉"响应头不在白名单就退回声明类型"那类写法 —— 两边都是
    image/*,兜底兜到的正是那个会炸的值。
    """
    respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=WEBP, headers={"content-type": "image/*"}))
    files = await download([{"contentType": "image/*", "contentUrl": GOOD_URL}])
    assert [f.content_type for f in files] == ["image/webp"]
    # 下游契约本身,而不是它的近似:构造不出来就等于 turn 被打死
    assert [ImageInput(data=f.content, mime_type=f.content_type).mime_type
            for f in files] == ["image/webp"]


@respx.mock
async def test_wildcard_attachment_survives_the_whole_pipeline():
    """端到端:一张真实形态的 Teams 内联图片穿过 select_images → _fetch。

    重演冒烟里的那条消息:Teams 下发一个 text/html 卡片 + 一个 image/*
    图片附件。事故当时 image_count 是 0;这条钉住它必须是 1,且带具体类型。

    连着断言 _raw_image_count 是为了钉死接缝:skipped = raw - len(images)
    必须算出 0,用户才不会再收到那句"(另有 1 张图片未能获取)"。
    只断言 len(files) == 1 抓不到这半边 —— 事故里下载器与计数器口径不一致,
    恰恰是两侧都单独"正确"却对不上。
    """
    attachments = [
        {"contentType": "text/html", "contentUrl": GOOD_URL},   # Teams 的伴生卡片
        {"contentType": "image/*", "contentUrl": GOOD_URL},
    ]
    respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=JPEG, headers={"content-type": "image/jpeg"}))

    files = await download(attachments)
    assert [f.content_type for f in files] == ["image/jpeg"]
    assert files[0].content == JPEG
    # 接缝:下载到的张数必须补齐 _raw_image_count 数到的张数
    raw = _raw_image_count(_activity_to_dict(attachments_activity(attachments)))
    assert raw - len(files) == 0


@respx.mock
@respx.mock
async def test_one_failed_image_does_not_drop_the_others():
    """一张图失败不能连累其余的,也不能只下第一张就收工。

    在此之前所有下载用例都只喂一张图,于是整个循环语义没有被钉住:
    实测"失败即 break"与"成功一张即 break"两种写法都能 31 passed 全绿。
    前者让一次瞬时故障吃掉后面的图(违背"图片是增强,不能成为新的失败源"),
    后者让 MAX_IMAGES=4 形同虚设 —— 两者都静默丢图,用户无从察觉。

    样本刻意排成 [失败, 成功, 成功]:失败排在最前,才能同时判别这两种
    break;若把失败放在最后,两种写法都会给出相同的可观测结果。
    """
    base = "https://smba.trafficmanager.net/amer/v3/attachments"
    first, second, third = f"{base}/1", f"{base}/2", f"{base}/3"
    respx.get(first).mock(return_value=httpx.Response(404))
    for url in (second, third):
        respx.get(url).mock(return_value=httpx.Response(
            200, content=PNG, headers={"content-type": "image/png"}))

    files = await download([{"contentType": "image/png", "contentUrl": u}
                            for u in (first, second, third)])
    assert [f.content_url for f in files] == [second, third]


@respx.mock
async def test_response_content_type_with_parameters_is_parsed():
    """content-type 带参数是常见形态,必须剥掉 ";charset=..." 再比对。

    不剥的话 "image/png; charset=utf-8" 落不进白名单,会悄悄退回声明类型 ——
    此处声明的是 gif,于是一张 PNG 被标成 image/gif 发给 Azure OpenAI。
    """
    respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=PNG, headers={"content-type": "image/png; charset=utf-8"}))
    files = await download([{"contentType": "image/gif", "contentUrl": GOOD_URL}])
    assert files[0].content_type == "image/png"


async def test_non_teams_channel_skipped():
    """非 msteams 频道一次请求都不发。

    断言必须落在"有没有发出请求"上,只断言返回 [] 抓不到东西:不挂
    @respx.mock 时,去掉频道守卫会真的去打 smba.trafficmanager.net,而
    连不通(被 _fetch 的 except 吞掉)和 401(非 200 分支)都返回 [],
    测试照样全绿。实测:去掉守卫后该用例真的发出了带 Bearer 的请求、
    收到线上 401,仍然 PASSED —— 顺带把 token 递给了真实网络。
    """
    route = respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=PNG, headers={"content-type": "image/png"}))
    assert await download(IMAGE_ATTACHMENT, channel_id="webchat") == []
    assert route.called is False


async def test_no_attachments_makes_no_token_call():
    """无图消息不取 token。

    观测点必须是"取 token 这一步有没有被调用",不能靠假 connection manager
    抛异常来表达:AssertionError 也是 Exception,会被 _access_token 外面的
    try/except 吞掉,返回同样的 [],于是"有短路"与"无短路"两条分支产生
    完全相同的可观测结果(实测:去掉短路后 28 passed,变异存活)。
    """
    token_calls = []

    class RecordingConnectionManager:
        def get_token_provider_from_activity(self, identity, activity):
            token_calls.append(activity)
            return StubProvider()

    activity = attachments_activity([])
    files = await TeamsImageDownloader(
        RecordingConnectionManager()).download_files(FakeContext(activity))
    assert files == []
    assert token_calls == []


async def test_malformed_url_does_not_escape_as_an_exception():
    """download_files 必须永不抛 —— SDK 那侧没有任何网接着。

    agent_application.py 的 _handle_file_downloads 是裸
    `await file_downloader.download_files(context)`,外层 _on_turn 只
    `except ApplicationError`,所以 ValueError / AttributeError / httpx.* 会一路
    穿透到 aiohttp。后果不是"这张图没下到",而是整个 turn 死掉:没有回复、
    没有 FALLBACK_MESSAGE、没有 typing 指示器,连 turn_state.save() 都被跳过 ——
    比图片丢失严重得多,而且从用户视角完全静默。

    'https://[abc' 是实测挑的:Attachment 对 content_url 不做任何校验照单全收,
    urlparse 到它就抛 ValueError('Invalid IPv6 URL')。select_images 里那段
    "永不抛异常"的注释守的是 urlparse(None) —— 守不住格式错误的字符串。

    断言必须是"返回 []"而不是 pytest.raises 的反面:静默吞掉异常但返回 None
    会让 SDK 的 input_files.extend(None) 再炸一次,等于没修。
    """
    assert await download([{"contentType": "image/png",
                            "contentUrl": "https://[abc"}]) == []
