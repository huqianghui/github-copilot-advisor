"""Match a known source URL in Markdown without matching a longer URL."""
import re


def has_citation(markdown: str, url: str) -> bool:
    return bool(re.search(
        r"(?<![\w/=?&])" + re.escape(url)
        + r"""(?=$|[\s<>"'`)\]\u3002\uff0c\u3001\uff1b\uff1a\uff01\uff1f]|[.,;!?](?=$|\s))""",
        markdown,
    ))
