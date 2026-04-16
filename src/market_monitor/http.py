from __future__ import annotations

import logging
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class HttpClient:
    def __init__(self, user_agent: str | None = None, timeout_seconds: int = 20) -> None:
        self.user_agent = user_agent or "OpenClawPriceMonitor/0.1 (+https://local-dev)"
        self.timeout_seconds = timeout_seconds

    def get_text(
        self,
        url: str,
        *,
        params: dict[str, str | list[str]] | None = None,
        encoding: str | None = "utf-8",
        headers: dict[str, str] | None = None,
    ) -> str:
        target = url
        if params:
            query = urlencode(params, doseq=True)
            separator = "&" if "?" in url else "?"
            target = f"{url}{separator}{query}"

        request_headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Cache-Control": "no-cache",
        }
        if headers:
            request_headers.update(headers)

        request = Request(
            target,
            headers=request_headers,
        )
        logger.debug("HTTP GET target=%s timeout_seconds=%s", target, self.timeout_seconds)
        with urlopen(request, timeout=self.timeout_seconds) as response:
            payload = response.read()
            selected_encoding = encoding or response.headers.get_content_charset() or "utf-8"
            text = payload.decode(selected_encoding, errors="replace")
            logger.debug(
                "HTTP GET completed target=%s status=%s bytes=%s encoding=%s",
                target,
                getattr(response, "status", "unknown"),
                len(payload),
                selected_encoding,
            )
            return text
