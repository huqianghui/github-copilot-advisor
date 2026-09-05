# channels/teams/tests/test_downloader.py
"""select_images:附件筛选、格式白名单与 host 白名单(图片输入 spec §5)。

只覆盖纯函数的选取逻辑;实际下载(重定向、超时、大小上限、token)属 Task 7。
"""
import pytest
from microsoft_agents.activity import Activity

from teams_adapter.downloader import MAX_IMAGES, select_images

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
