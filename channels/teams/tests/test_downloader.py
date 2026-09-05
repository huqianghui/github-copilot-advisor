# channels/teams/tests/test_downloader.py
"""select_images:附件筛选、格式白名单与 host 白名单(图片输入 spec §5)。

只覆盖纯函数的选取逻辑;实际下载(重定向、超时、大小上限、token)属 Task 7。
"""
import httpx
import pytest
import respx
from microsoft_agents.activity import Activity

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


def test_rejects_formats_azure_openai_cannot_read():
    """SVG/BMP/TIFF 过不了 Azure OpenAI。放行它们会让请求在 API 层报 400,
    进而触发剥图降级 —— 用户收到的将是与真实原因无关的提示。"""
    for bad in ("image/svg+xml", "image/bmp", "image/tiff",
                "image/vnd.microsoft.icon"):
        assert urls_for([{"contentType": bad, "contentUrl": GOOD_URL}]) == []


def test_returns_content_type_alongside_url():
    """content_type 要带出去:_fetch 在响应头不可信时拿它兜底。"""
    assert select_images(attachments_activity(
        [{"contentType": "image/jpeg", "contentUrl": GOOD_URL}]).attachments
    ) == [(GOOD_URL, "image/jpeg")]


def test_content_type_is_normalized_to_lowercase():
    assert select_images(attachments_activity(
        [{"contentType": "IMAGE/PNG", "contentUrl": GOOD_URL}]).attachments
    ) == [(GOOD_URL, "image/png")]


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
async def test_never_requests_non_allowlisted_host():
    """安全回归:非白名单 host 一次请求都不能发出。"""
    route = respx.get(EVIL_URL).mock(return_value=httpx.Response(200, content=PNG))
    files = await download([{"contentType": "image/png", "contentUrl": EVIL_URL}])
    assert files == []
    assert route.called is False


@respx.mock
async def test_does_not_follow_redirects():
    """安全回归:跟随重定向会把 Authorization 带到重定向目标。"""
    first = respx.get(GOOD_URL).mock(return_value=httpx.Response(
        302, headers={"location": EVIL_URL}))
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
async def test_unusable_response_content_type_falls_back_to_declared():
    """响应头不可信时用 select_images 已校验过的声明类型,
    不能无脑塞 image/png —— 那会把 GIF 谎报成 PNG。"""
    respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=PNG, headers={"content-type": "application/octet-stream"}))
    files = await download([{"contentType": "image/gif", "contentUrl": GOOD_URL}])
    assert files[0].content_type == "image/gif"


@respx.mock
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
