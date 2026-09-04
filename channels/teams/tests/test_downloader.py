# channels/teams/tests/test_downloader.py
"""TeamsImageDownloader:筛选、白名单与下载安全(图片输入 spec §5)。"""
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


def test_rejects_lookalike_host_suffix():
    """trafficmanager.net 是共享命名空间,后缀匹配会放进第三方域名。

    四个变体各钉死一种错误写法,少一个都会漏:
      1. endswith("trafficmanager.net") —— 只有 evil.trafficmanager.net 能证伪
      2. endswith("smba.trafficmanager.net") —— 缺点边界,evilsmba 能混进来
      3. "trafficmanager.net" in host —— 子串匹配
      4. 用 netloc 而非 hostname —— userinfo 段可伪装成白名单 host
    注意 1 和 2 都不认 attacker.com 那条(它以 attacker.com 结尾),
    所以只靠第 3 条无法区分精确相等与后缀匹配。
    """
    for lookalike in (
        "https://evil.trafficmanager.net/x",
        "https://evilsmba.trafficmanager.net/x",
        "https://evil-smba.trafficmanager.net.attacker.com/x",
        "https://smba.trafficmanager.net@evil.com/x",
    ):
        assert urls_for(
            [{"contentType": "image/png", "contentUrl": lookalike}]
        ) == [], lookalike


def test_skips_attachment_without_url():
    assert urls_for([{"contentType": "image/png"}]) == []


def test_caps_image_count():
    many = [{"contentType": "image/png", "contentUrl": GOOD_URL}] * 10
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
