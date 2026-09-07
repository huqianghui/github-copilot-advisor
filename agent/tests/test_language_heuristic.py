"""`is_mostly_chinese` 的纯函数单元测试(不需要凭据,默认套件里跑)。

这个启发式只被 test_eval_behavior.py 的 reply_language 断言使用,而那整个文件是
integration 标记、默认不跑 —— 也就是说改动前它在默认套件里零覆盖。这里补上。

样本的选取原则:必须落在**临界区**。一段汉字数为 0 的英文回答对新旧两版实现
都返回 False,证明不了任何东西;所以下面的英文样本都带真实比例的中文引用,
中文样本都带真实比例的英文术语/代码/URL。
"""
import re

from test_eval_behavior import _prose_only, is_mostly_chinese


def _legacy_char_ratio(text: str) -> bool:
    """改动前的实现,只作回归见证:用来证明样本确实能区分新旧两版,
    而不是"把断言放宽到大家都通过"。"""
    han = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[a-zA-Z]", text))
    return han > latin * 0.5


def _morphemes_without_stripping(text: str) -> bool:
    """只换了语素计数、但不剥离语言中立内容的半成品。第二个见证:用来证明
    剥离那一步确实改变了判定结论,而不只是"剥离函数自己能跑通"。"""
    han = len(re.findall(r"[一-鿿]", text))
    latin_words = len(re.findall(r"[a-zA-Z]+", text))
    return han > latin_words


def _counts(text: str) -> tuple[int, int]:
    """新实现实际比较的两个量:剥离后的汉字数与拉丁词数。"""
    prose = _prose_only(text)
    return (len(re.findall(r"[一-鿿]", prose)),
            len(re.findall(r"[a-zA-Z]+", prose)))


# ---------------------------------------------------------------------------
# 真实样本:2026-09-05 跑 mcp-config 用例three次,第三次的真实回复(前 12 行原文)。
# 这一版无可争议是中文,但旧实现判 False —— 就是它让 mcp-config 三跑二败。
# ---------------------------------------------------------------------------
REAL_ZH_REPLY = """结论（简短）
- 在 Copilot 里配置 MCP server 有三种常见入口：仓库/组织设置（Copilot → MCP servers）、IDE 内的 MCP 配置面板（VS Code / Visual Studio / IntelliJ 的 Copilot 插件）和 Copilot CLI（~/.copilot/mcp-config.json）。
- 私密凭据（PAT/密钥）不要写入代码或 repo 文件，推荐放在 GitHub secrets / OS 密钥库 / 企业秘密管理器，并尽量用最小权限与定期轮换。下面给出可执行步骤与安全建议，并附官方文档链接。

具体步骤（按场景）
1) 在 GitHub（云端）为仓库或组织添加 MCP 配置（常用于 Copilot Cloud Agent）
   - 进入仓库 → Settings → Copilot → MCP servers，添加 MCP 配置并保存。参考：官方说明 https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/configure-mcp-servers
   - 为 agent 添加一个名为 COPILOT_MCP_GITHUB_PERSONAL_ACCESS_TOKEN 的 secret（值为你的 PAT），可以在组织级或仓库级配置。官方页有这一步的说明（见上文链接）。

2) 在 IDE（本地）中配置 MCP server
   - VS Code / Visual Studio / JetBrains 的 Copilot 插件一般在 Copilot Chat 的“Configure Tools / Configure MCP server”窗口中填写服务器信息；Visual Studio 对 MCP server 有“信任”提示（可配置）。参考：
     - 设置 GitHub MCP Server 操作说明（示例/步骤）: https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp-in-your-ide/set-up-the-github-mcp-server"""

# 真实样本:同日跑 agent-english-question 用例的真实英文回复(节选原文)。
REAL_EN_REPLY = """Short answer
- Subagents are lightweight, isolated agents the main Copilot agent can spawn to handle a focused subtask (research, review, small code edits). The parent agent delegates the work, the subagent runs in its own context with its own prompt and tool restrictions, then returns a summary/results which the parent synthesizes back into the main conversation.

How it works (practical flow)
1. Decision to delegate: the main agent (not usually the user) decides a portion of the request is better handled separately (e.g., deep research, a focused code edit, or an automated reviewer).
2. Invocation: the main agent calls runSubagent (or the platform's equivalent) to start the subagent. The run can be synchronous or parallel; the parent can choose the model for the subagent or let it inherit the parent's model.
3. Isolated context: the subagent runs in a context-isolated session with its own system prompt, tool permissions (read-only vs edit, grep, view, etc.), and optionally different model choices."""

# 构造样本(非真实输出):一篇英文回答,但逐字引用了 4 段中文报错和一条中文菜单路径。
# 这是**反作弊**的主力样本 —— 它落在 0.5*词数 与 1.0*词数 之间,所以任何把阈值
# 放宽到 `han > 词数 * 0.5` 或更松的实现都会把它误判成中文,而被采用的
# `han > 词数` 仍然正确判 False。汉字数为 0 的英文样本做不到这一点。
EN_REPLY_QUOTING_ZH_ERRORS = """Short answer: all three of those are the same sign-in
loop, localized into Chinese. Your Copilot licence is fine, so do not open a billing
ticket for this one.

The strings you pasted are the zh-CN builds of errors that the extension already
emits in English:
- 无法验证您的 GitHub Copilot 订阅状态，请重新登录后再试
- 登录已过期，请在浏览器中完成授权后返回编辑器
- 网络连接被重置，请检查代理设置或联系您的网络管理员
- 代理服务器要求身份验证，请在系统设置中提供凭据后重试该操作

The first two are emitted by the auth handshake and the third one comes from the
proxy layer, which means the underlying cause is almost always a stale cached token
behind a corporate proxy rather than a seat assignment problem.

The menu path in your screenshot, 文件 → 首选项 → 设置 → 扩展, is simply File,
Preferences, Settings, Extensions in an English build, so you can follow the English
documentation step by step without translating anything.

To clear it: sign out from the account menu in the bottom left, delete the cached
credential from the OS keychain, restart the editor, and sign in again. If your proxy
rewrites TLS you also need to trust its root certificate before the handshake will
succeed.

See https://docs.github.com/en/copilot/troubleshooting for the full error matrix.
"""

# 构造样本:一句正常的中文技术指引,英文术语密度已经把旧实现顶翻。
ZH_ONE_LINER = "在 VS Code 中打开设置，搜索 GitHub Copilot。"

# 构造样本:中文回答,但代码块与 URL 的分量压过正文。用来钉住**剥离**这一步 ——
# 不剥离时 han=41 / 拉丁词=51 判英文(错),剥离后 41 / 3 判中文(对)。
ZH_REPLY_CODE_HEAVY = """在 VS Code 里配置 MCP 服务器，把密钥交给输入变量，不要写进仓库。

```json
{
  "inputs": [
    { "type": "promptString", "id": "token", "description": "GitHub token", "password": true }
  ],
  "servers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${input:token}" }
    }
  }
}
```

保存后重启编辑器即可生效。参考 https://code.visualstudio.com/docs/copilot/chat/mcp-servers
和 [官方说明](https://docs.github.com/en/copilot/customizing-copilot/using-model-context-protocol)。
"""

# 构造样本:英文回答,但贴出来的代码块里全是中文注释(转述同事的配置)。
# 剥离的反方向证明 —— 不剥离时 han=92 / 拉丁词=61 判中文(错),剥离后 0 / 54 判英文(对)。
EN_REPLY_WITH_ZH_CODE_COMMENTS = """Short answer: put the token in an input variable, never in the repo.
The snippet below is the config your teammate pasted, with their comments left
as they wrote them, so you can diff it against yours line by line.

```jsonc
{
  // 这里填写服务器名称，必须与调用方保持一致，否则解析的时候会直接失败
  "servers": {
    // 凭据不要直接写在这里，改用输入变量引用，提交之前务必再检查一遍
    // 如果团队里有人已经把明文密钥推上去了，记得先吊销再重新签发一把新的
    "github": { "env": { "TOKEN": "${input:token}" } }
  }
}
```

Restart the editor once you have saved it and the server will show up.
"""


# ---------------------------------------------------------------------------
# 1. 中文回答判真 —— 含代码块/URL 的技术回答
# ---------------------------------------------------------------------------

def test_real_chinese_reply_with_urls_is_chinese():
    """真实的 mcp-config 中文回复:新实现判真。"""
    assert is_mostly_chinese(REAL_ZH_REPLY)


def test_real_chinese_reply_is_the_one_the_old_ratio_rejected():
    """回归见证:同一段文本旧实现判 False。样本因此对新旧两版有判别力,
    不是"放宽之后大家都通过"。"""
    assert _legacy_char_ratio(REAL_ZH_REPLY) is False
    assert is_mostly_chinese(REAL_ZH_REPLY) is True


def test_chinese_one_liner_dense_with_english_terms():
    """"在 VS Code 中打开设置，搜索 GitHub Copilot。" —— 无争议的中文句子,
    旧实现同样判不出来。"""
    assert _legacy_char_ratio(ZH_ONE_LINER) is False
    assert is_mostly_chinese(ZH_ONE_LINER) is True


def test_chinese_reply_whose_code_block_outweighs_the_prose():
    """代码块比正文还重时仍应判中文。

    这条同时钉住**剥离**那一步:不剥离的话 han=41 / 拉丁词=51,连语素计数版
    也会把它判成英文;剥离之后是 41 / 3。所以它证明的是剥离改变了结论,
    而不只是"_prose_only 自己能跑通"。
    """
    assert _legacy_char_ratio(ZH_REPLY_CODE_HEAVY) is False, "旧实现本就判错"
    assert _morphemes_without_stripping(ZH_REPLY_CODE_HEAVY) is False, \
        "样本代码块不够重,证明不了剥离的必要性"
    assert is_mostly_chinese(ZH_REPLY_CODE_HEAVY) is True


# ---------------------------------------------------------------------------
# 2. 英文回答判假 —— 反作弊主力
# ---------------------------------------------------------------------------

def test_real_english_reply_is_not_chinese():
    assert is_mostly_chinese(REAL_EN_REPLY) is False


def test_english_reply_quoting_chinese_errors_is_not_chinese():
    """英文回答里引用了 4 段中文报错,仍必须判 False。"""
    assert is_mostly_chinese(EN_REPLY_QUOTING_ZH_ERRORS) is False


def test_english_reply_whose_code_block_is_full_of_chinese_comments():
    """英文回答贴了一段满是中文注释的配置,不能因此被判成中文。

    剥离的反方向见证:不剥离时 han=92 / 拉丁词=61 会判成中文(错)。
    """
    assert _morphemes_without_stripping(EN_REPLY_WITH_ZH_CODE_COMMENTS) is True, \
        "样本中文注释不够多,证明不了剥离在英文方向上的必要性"
    assert is_mostly_chinese(EN_REPLY_WITH_ZH_CODE_COMMENTS) is False


def test_english_sample_sits_in_the_discriminating_band():
    """钉住上面那条样本的判别力:汉字数必须落在(词数*0.5, 词数)之间。

    落在这个区间,才意味着任何把阈值放宽到 `han > 词数 * 0.5` 及以下的实现
    都会被它抓住(误判成中文),而被采用的 `han > 词数` 判 False。若日后有人
    编辑了样本文本让汉字变少,这条断言会先失败,提醒样本已失去反作弊能力。
    """
    han, latin_words = _counts(EN_REPLY_QUOTING_ZH_ERRORS)
    assert han > latin_words * 0.5, "样本汉字太少,抓不住放宽到 0.5 的实现"
    assert han < latin_words, "样本汉字太多,已经不是一篇英文回答了"


# ---------------------------------------------------------------------------
# 3. 剥离逻辑本身
# ---------------------------------------------------------------------------

def test_prose_only_strips_fenced_code_block():
    text = "说明文字\n```python\nprint('hello world from copilot')\n```\n收尾"
    prose = _prose_only(text)
    assert "hello" not in prose and "print" not in prose
    assert "说明文字" in prose and "收尾" in prose


def test_prose_only_strips_inline_code():
    text = "请在 `settings.json` 里加上 `github.copilot.enable` 这一项。"
    prose = _prose_only(text)
    assert "settings" not in prose and "copilot" not in prose
    assert "请在" in prose and "这一项" in prose


def test_prose_only_strips_markdown_link_target_but_keeps_link_text():
    """链接目标是语言中立的,链接文字是作者写的,必须留下。"""
    text = "参考[官方文档](https://docs.github.com/en/copilot/using-github-copilot)。"
    prose = _prose_only(text)
    assert "docs.github.com" not in prose
    assert "官方文档" in prose


def test_prose_only_strips_bare_url():
    text = "详见 https://code.visualstudio.com/docs/copilot/chat/mcp-servers 这一页。"
    prose = _prose_only(text)
    assert "visualstudio" not in prose
    assert "详见" in prose and "这一页" in prose


def test_bare_url_strip_does_not_eat_the_chinese_that_follows_it():
    """中文句子在 URL 后不加空格。若用 `https?://\\S+` 收尾,句号后面的整句中文
    都会被当成 URL 的一部分吃掉,汉字被系统性少算。"""
    text = "参考 https://code.visualstudio.com/docs/copilot/mcp。配置完成后重启编辑器即可生效。"
    prose = _prose_only(text)
    assert "配置完成后重启编辑器即可生效" in prose
    han, _ = _counts(text)
    assert han == len(re.findall(r"[一-鿿]", text)), "URL 剥离吃掉了正文汉字"


def test_empty_text_is_not_chinese():
    """没有 reply_language 的用例(纯图片消息)不会走到这里,但空串不该炸。"""
    assert is_mostly_chinese("") is False
