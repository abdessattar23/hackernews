import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from agent.ai_client import HackClubAIClient
from agent.config import load_config
from agent.s3_store import S3Store
from agent import db
from agent import thn


def _slugify(value: str) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r"[^a-z0-9\s-]", "", v)
    v = re.sub(r"[\s-]+", "-", v).strip("-")
    return v or "post"


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


async def _fetch_source_bundle(client: httpx.AsyncClient, item: Dict[str, Any]) -> Dict[str, Any]:
    html = await thn.fetch_article_html(client, item["url"])
    article = thn.parse_article(html, item["url"])
    bundle = dict(item)
    bundle["article"] = article
    return bundle


async def run_once() -> int:
    cfg = load_config()
    if not cfg.ai_api_key:
        raise RuntimeError("Missing AI_API_KEY (or HACKCLUB_API_KEY)")
    if not cfg.s3_bucket:
        raise RuntimeError("Missing S3_BUCKET")

    store = S3Store(bucket=cfg.s3_bucket, prefix=cfg.s3_prefix)

    conn = db.connect(cfg.db_path)
    db.init(conn)

    http = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={"User-Agent": "Mozilla/5.0"},
        follow_redirects=True,
    )

    ai = HackClubAIClient(base_url=cfg.ai_base_url, api_key=cfg.ai_api_key)

    processed = 0
    today = datetime.utcnow().strftime("%Y-%m-%d")
    base_out = Path(cfg.output_dir) / today

    try:
        listing_html = await thn.fetch_listing_html(http)
        listing = thn.parse_listing(listing_html)
        candidates = thn.pick_candidates(listing, cfg.max_items)

        for item in candidates:
            url = item.get("url")
            if not url:
                continue
            if db.was_completed(conn, url):
                continue

            title = item.get("title")
            db.mark_started(conn, url, title)

            slug = _slugify(title or url)
            out_dir = base_out / slug

            try:
                source = await _fetch_source_bundle(http, item)
                _write_text(out_dir / "source.json", json.dumps(source, ensure_ascii=False, indent=2))

                blog_en = await ai.generate_blog_markdown(cfg.blog_model, source)
                _write_text(out_dir / "blog_en.md", blog_en)

                blog_darija = await ai.translate_to_darija(cfg.darija_model, blog_en)
                _write_text(out_dir / "blog_darija.md", blog_darija)

                img_prompt = (
                    "Create an editorial-style illustration for this Moroccan Darija cybersecurity blog post. "
                    "No text in the image. Modern, clean style.\n\n"
                    f"DARJA_POST:\n{blog_darija[:4000]}\n"
                )

                image_bytes, image_caption = await ai.generate_illustration(cfg.image_model, img_prompt, aspect_ratio="16:9")
                if image_bytes:
                    _write_bytes(out_dir / "illustration.png", image_bytes)

                meta = {
                    "source_url": url,
                    "generated_at": int(time.time()),
                    "models": {
                        "blog_model": cfg.blog_model,
                        "darija_model": cfg.darija_model,
                        "image_model": cfg.image_model,
                    },
                    "image_caption": image_caption,
                }
                _write_text(out_dir / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))

                s3_prefix = f"{today}/{slug}"
                store.put_text(f"{s3_prefix}/source.json", (out_dir / "source.json").read_text(encoding="utf-8"), "application/json")
                store.put_text(f"{s3_prefix}/blog_en.md", blog_en, "text/markdown; charset=utf-8")
                store.put_text(f"{s3_prefix}/blog_darija.md", blog_darija, "text/markdown; charset=utf-8")
                store.put_text(f"{s3_prefix}/meta.json", json.dumps(meta, ensure_ascii=False, indent=2), "application/json")
                if image_bytes:
                    store.put_bytes(f"{s3_prefix}/illustration.png", image_bytes, "image/png")

                db.mark_completed(conn, url, s3_prefix)
                processed += 1

            except Exception as e:
                db.mark_failed(conn, url, str(e))

        return processed

    finally:
        await http.aclose()
        await ai.close()
        conn.close()


if __name__ == "__main__":
    count = asyncio.run(run_once())
    print(count)
