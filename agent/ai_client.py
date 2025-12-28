import asyncio
import base64
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx


logger = logging.getLogger("hn_agent.ai")


class HackClubAIClient:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 240.0, max_retries: int = 3) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=20.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        last_exc: Optional[BaseException] = None

        for attempt in range(self._max_retries + 1):
            try:
                r = await self._client.post(self._base_url, json=payload)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 429 or (isinstance(status, int) and 500 <= status < 600):
                    last_exc = e
                else:
                    raise
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e

            if attempt >= self._max_retries:
                assert last_exc is not None
                raise last_exc

            wait_s = min(2 ** attempt, 30)
            logger.warning(
                "ai_retry attempt=%s/%s wait_s=%s error=%s",
                attempt + 1,
                self._max_retries,
                wait_s,
                type(last_exc).__name__ if last_exc else "unknown",
            )
            await asyncio.sleep(wait_s)

    @staticmethod
    def _extract_text(resp: Dict[str, Any]) -> str:
        choices = resp.get("choices")
        if isinstance(choices, list) and choices:
            msg = (choices[0] or {}).get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts: List[str] = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text" and isinstance(p.get("text"), str):
                        parts.append(p["text"])
                return "\n".join(parts).strip()
        return ""

    async def generate_blog_markdown(self, model: str, source: Dict[str, Any]) -> str:
        title = source.get("title") or ""
        url = source.get("url") or ""
        summary = source.get("description") or ""
        text = source.get("article", {}).get("text") or ""

        prompt = (
            "You are a professional cybersecurity blog writer. "
            "Write a complete long-form blog post in Markdown based on the source below. "
            "Include: a strong headline, short TL;DR, sections with headings, and a conclusion. "
            "Keep factual accuracy: do not invent details not present in the source. "
            "If information is missing, state it clearly. "
            "Add a 'Source' section with the original URL at the end.\n\n"
            f"SOURCE_TITLE: {title}\n"
            f"SOURCE_URL: {url}\n"
            f"SOURCE_SUMMARY: {summary}\n\n"
            "SOURCE_TEXT:\n"
            f"{text}\n"
        )

        resp = await self.chat(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        return self._extract_text(resp).strip()

    async def translate_to_darija(self, model: str, markdown: str) -> str:
        prompt = (
            "Translate the following Markdown blog post into Moroccan Arabic (Darija). "
            "Keep Markdown structure (headings, bullet points, links). "
            "Do not translate URLs. Preserve code blocks exactly.\n\n"
            f"MARKDOWN:\n{markdown}\n"
        )
        resp = await self.chat(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        return self._extract_text(resp).strip()

    async def generate_illustration(self, model: str, prompt: str, aspect_ratio: str = "16:9") -> Tuple[Optional[bytes], str]:
        resp = await self.chat(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "modalities": ["image", "text"],
                "image_config": {"aspect_ratio": aspect_ratio},
                "stream": False,
            }
        )

        text = self._extract_text(resp).strip()
        image_bytes = self._extract_image_bytes(resp)
        if image_bytes is None:
            image_url = self._extract_image_http_url(resp)
            if image_url:
                image_bytes = await self._download_image(image_url)

        return image_bytes, text

    def _extract_image_bytes(self, resp: Dict[str, Any]) -> Optional[bytes]:
        choices = resp.get("choices")
        if not isinstance(choices, list) or not choices:
            return None

        msg = (choices[0] or {}).get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            return None

        for p in content:
            if not isinstance(p, dict):
                continue

            if p.get("type") == "image_url":
                image_url = p.get("image_url") or {}
                url = image_url.get("url")
                if isinstance(url, str) and url.startswith("data:"):
                    return self._decode_data_url(url)

            if p.get("type") == "image" and isinstance(p.get("data"), str):
                try:
                    return base64.b64decode(p["data"], validate=True)
                except Exception:
                    return None

        return None

    def _extract_image_http_url(self, resp: Dict[str, Any]) -> Optional[str]:
        choices = resp.get("choices")
        if not isinstance(choices, list) or not choices:
            return None

        msg = (choices[0] or {}).get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            return None

        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") != "image_url":
                continue

            image_url = p.get("image_url") or {}
            url = image_url.get("url")
            if isinstance(url, str) and (url.startswith("https://") or url.startswith("http://")):
                return url

        return None

    async def _download_image(self, url: str) -> Optional[bytes]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                r = await client.get(url, follow_redirects=True)
                r.raise_for_status()
                return r.content
        except Exception:
            return None

    @staticmethod
    def _decode_data_url(data_url: str) -> Optional[bytes]:
        try:
            header, b64 = data_url.split(",", 1)
            if ";base64" not in header:
                return None
            return base64.b64decode(b64, validate=True)
        except Exception:
            return None
