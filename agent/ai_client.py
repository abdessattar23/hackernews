import asyncio
import base64
import json
import logging
import re
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

    @staticmethod
    def _preview(text: str, limit: int = 300) -> str:
        t = (text or "").replace("\r", " ").replace("\n", " ").strip()
        if len(t) <= limit:
            return t
        return t[:limit] + "..."

    async def chat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        last_exc: Optional[BaseException] = None

        for attempt in range(self._max_retries + 1):
            try:
                model = payload.get("model")
                logger.info("running ai_request model=%s", model)
                r = await self._client.post(self._base_url, json=payload)
                status = r.status_code

                body_text = ""
                try:
                    data = r.json()
                except Exception:
                    data = None
                    body_text = (r.text or "")

                if status >= 400:
                    if not body_text:
                        try:
                            body_text = json.dumps(data, ensure_ascii=False)
                        except Exception:
                            body_text = ""
                    logger.error(
                        "response status: %s, message: %s",
                        status,
                        self._preview(body_text, 800),
                    )
                    r.raise_for_status()

                if isinstance(data, dict):
                    msg = self._extract_text(data)
                    logger.info("response status: %s, message: %s", status, self._preview(msg))
                    return data

                logger.info("response status: %s, message: %s", status, self._preview(body_text))
                return {}
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

    @staticmethod
    def _extract_json_from_text(text: str) -> Dict[str, Any]:
        raw = (text or "").strip()
        if not raw:
            return {}

        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    async def pick_best_article_for_linkedin(self, model: str, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        prompt = (
            "You are a LinkedIn editor for a Moroccan tech/cybersecurity audience. "
            "Pick the single best article to post today for maximum engagement and usefulness.\n\n"
            "Return ONLY valid JSON with keys: selected_index (0-based integer), reason_short (string).\n\n"
            f"CANDIDATES_JSON:\n{json.dumps(candidates, ensure_ascii=False)}\n"
        )

        resp = await self.chat(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        text = self._extract_text(resp)
        return self._extract_json_from_text(text)

    async def pick_linkedin_template(self, model: str, blog_text: str, templates_text: str) -> Dict[str, Any]:
        prompt = (
            "You are a LinkedIn copy chief. Select the best template from the provided list for the blog below. "
            "Prefer Darija-first bilingual output.\n\n"
            "Return ONLY valid JSON with keys: template_number (integer 1-9), template_name (string), reason_short (string).\n\n"
            f"TEMPLATES:\n{templates_text}\n\n"
            f"BLOG_TEXT:\n{blog_text[:6000]}\n"
        )

        resp = await self.chat(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        text = self._extract_text(resp)
        return self._extract_json_from_text(text)

    async def generate_linkedin_draft(
        self,
        model: str,
        blog_darija: str,
        blog_en: str,
        template_number: int,
        templates_text: str,
        link_url: str,
        brand: str,
    ) -> Dict[str, Any]:
        prompt = (
            "You write high-performing LinkedIn posts for Moroccan tech/cybersecurity. "
            "Generate ONE bilingual LinkedIn post (primary Arabic Darija, short English secondary) based on the blog.\n\n"
            "Rules:\n"
            "- Do NOT include the URL in the post body. Put it in first_comment only.\n"
            "- The post body should tease value and invite discussion.\n"
            "- The first_comment must contain the link first on its own line, then the brand on a new line.\n"
            "- Keep it human, punchy, and readable on LinkedIn.\n\n"
            "- When writing in Darija, use Arabic Letters.\n\n"
            "Return ONLY valid JSON with keys:\n"
            "chosen_template_number (int), post_text (string), first_comment (string), hashtags (array of strings).\n\n"
            f"CHOSEN_TEMPLATE_NUMBER: {template_number}\n\n"
            f"TEMPLATES:\n{templates_text}\n\n"
            f"BLOG_DARIJA:\n{blog_darija[:8000]}\n\n"
            f"BLOG_ENGLISH:\n{blog_en[:4000]}\n\n"
            f"LINK_URL: {link_url}\n"
            f"BRAND: {brand}\n"
        )

        resp = await self.chat(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        text = self._extract_text(resp)
        return self._extract_json_from_text(text)

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

    async def generate_manga_prompts(self, model: str, blog_text: str) -> str:
        prompt = (
            "You are a creative prompt engineer for manga/anime image generation.\n\n"
            "Task: Convert a news blog article into 4 fully independent, colored manga image-generation prompts. "
            "Each prompt corresponds to a manga page depicting the incident in a funny, exaggerated, manga/anime style, "
            "with dialogue in Moroccan Darija.\n\n"
            "Instructions:\n\n"
            "1. Read the blog article carefully.\n"
            "2. Generate 4 independent prompts (Page 1–4), each standalone:\n"
            "   - Scene description:\n"
            "     - Background\n"
            "     - Characters (appearance, emotions, poses)\n"
            "     - Key objects/metaphors (coins, malware, mnemonics, warning symbols)\n"
            "     - Actions/comic exaggeration\n"
            "   - Dialogue in Moroccan Darija:\n"
            "     - Maximum 2 text blocks per page in Darija\n"
            "     - English text allowed for names, logos, numbers, or technical terms\n"
            "   - Visual style:\n"
            "     - Manga/anime cyberpunk\n"
            "     - Colored illustration\n"
            "     - Strong black ink lines, sketchy/gritty textures\n"
            "     - Color palette: dominant + accent colors (red for danger, neon for data, etc.)\n"
            "     - Lighting, cinematic framing, focus\n"
            "3. Main character rules:\n"
            "   - If male, always wearing sportif outfit of MAS (Maghreb Association of Sport) of Fez\n"
            "   - Maintain exaggerated facial expressions and gestures for humor\n"
            "4. Each prompt must be self-contained; can be generated independently.\n"
            "5. Output format: each prompt must be inside a fenced code block with language txt.\n\n"
            "Page 1 Prompt:\n```txt\n...\n```\n"
            "Page 2 Prompt:\n```txt\n...\n```\n"
            "Page 3 Prompt:\n```txt\n...\n```\n"
            "Page 4 Prompt:\n```txt\n...\n```\n\n"
            "Requirements:\n\n"
            "- Use metaphors for technical details: mnemonics, crypto theft, malware, backdoor, etc.\n"
            "- Exaggerated expressions, humorous tone, but story clearly reflects the news incident.\n"
            "- Colored manga only, no black-and-white.\n"
            "- Ultra-detailed, cinematic composition, poster-quality illustration.\n\n"
            "Input blog text:\n"
            f"{blog_text}\n"
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

        images = msg.get("images")
        if isinstance(images, list):
            for p in images:
                if not isinstance(p, dict):
                    continue

                image_url = p.get("image_url") if isinstance(p.get("image_url"), dict) else None
                url = (image_url or {}).get("url")
                if isinstance(url, str) and url.startswith("data:"):
                    return self._decode_data_url(url)

                if p.get("type") == "image" and isinstance(p.get("data"), str):
                    try:
                        return base64.b64decode(p["data"], validate=True)
                    except Exception:
                        return None

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

        images = msg.get("images")
        if isinstance(images, list):
            for p in images:
                if not isinstance(p, dict):
                    continue
                image_url = p.get("image_url") if isinstance(p.get("image_url"), dict) else None
                url = (image_url or {}).get("url")
                if isinstance(url, str) and (url.startswith("https://") or url.startswith("http://")):
                    return url

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
