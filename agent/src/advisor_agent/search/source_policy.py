"""Web source reputation, independent of whether a result answers the question."""
import posixpath
import re
from typing import Literal
from urllib.parse import unquote, urlsplit

from advisor_agent.search.models import SearchResult

WebSearchScope = Literal["trusted", "general"]
SourceConfidence = Literal["high", "low"]

TRUSTED_SEARCH_SITES = (
    "docs.github.com/en/copilot",
    "github.blog/changelog",
    "code.visualstudio.com/updates",
    "github.com/microsoft/vscode/issues",
    "github.com/orgs/community/discussions",
    "github.com/orgs/githubcopilotfaq",
    "github.com/githubcopilotfaq",
)


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def source_confidence(result: SearchResult) -> SourceConfidence:
    try:
        url = urlsplit(result.url)
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username or url.password or url.port not in (None, 80, 443)):
            return "low"
    except ValueError:
        return "low"
    path = posixpath.normpath(unquote(url.path)).lower()
    host = url.hostname.lower()
    copilot_mentioned = bool(re.search(
        r"(?<![a-z])copilot(?![a-z])", f"{result.title} {result.content}", re.I))
    if host == "docs.github.com":
        trusted = bool(re.match(r"^/[^/]+/copilot(?:/|$)", path))
    elif host == "github.blog":
        trusted = _under(path, "/changelog") and (
            "copilot" in path or copilot_mentioned)
    elif host == "code.visualstudio.com":
        trusted = _under(path, "/updates")
    elif host == "github.com":
        trusted = any(_under(path, prefix) for prefix in (
            "/microsoft/vscode/issues",
            "/orgs/githubcopilotfaq",
            "/githubcopilotfaq",
            "/orgs/community/discussions/categories/copilot-conversations",
        ))
        # Individual discussion URLs do not encode their category.
        if re.fullmatch(r"/orgs/community/discussions/\d+", path):
            trusted = copilot_mentioned
    else:
        trusted = False
    return "high" if trusted else "low"


def web_query(query: str, scope: WebSearchScope) -> str:
    if scope not in ("trusted", "general"):
        raise ValueError(f"invalid web search scope: {scope}")
    contextual_query = f"GitHub Copilot {query}"
    if scope == "general":
        return contextual_query
    sites = " OR ".join(f"site:{site}" for site in TRUSTED_SEARCH_SITES)
    return f"{contextual_query} ({sites})"
