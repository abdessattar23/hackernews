import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from agent.ai_client import HackClubAIClient
from agent.config import load_config
from agent.s3_store import S3Store
from agent import db
from agent import thn


logger = logging.getLogger("hn_agent")


def _configure_logging() -> None:
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if level > logging.DEBUG:
        for noisy in ("httpx", "httpcore", "botocore", "boto3", "s3transfer", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


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
    _configure_logging()
    cfg = load_config()
    if not cfg.ai_api_key:
        raise RuntimeError("Missing AI_API_KEY (or HACKCLUB_API_KEY)")
    if not cfg.s3_bucket:
        raise RuntimeError("Missing S3_BUCKET")

    logger.info(
        "agent_start s3_bucket=%s s3_prefix=%s output_dir=%s db_path=%s max_items=%s blog_model=%s darija_model=%s image_model=%s",
        cfg.s3_bucket,
        cfg.s3_prefix,
        cfg.output_dir,
        cfg.db_path,
        cfg.max_items,
        cfg.blog_model,
        cfg.darija_model,
        cfg.image_model,
    )

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
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base_out = Path(cfg.output_dir) / today

    try:
        t0 = time.monotonic()
        listing_html = await thn.fetch_listing_html(http)
        listing = thn.parse_listing(listing_html)
        candidates = thn.pick_candidates(listing, cfg.max_items)
        logger.info(
            "listing_fetched total=%s candidates=%s elapsed_s=%.3f",
            len(listing),
            len(candidates),
            time.monotonic() - t0,
        )

        for item in candidates:
            url = item.get("url")
            if not url:
                continue
            if db.was_completed(conn, url):
                logger.info("skip_completed url=%s", url)
                continue

            title = item.get("title")
            db.mark_started(conn, url, title)

            slug = _slugify(title or url)
            out_dir = base_out / slug

            try:
                item_t0 = time.monotonic()
                logger.info("process_start url=%s slug=%s", url, slug)
                source = await _fetch_source_bundle(http, item)
                _write_text(out_dir / "source.json", json.dumps(source, ensure_ascii=False, indent=2))
                logger.info("source_fetched url=%s", url)

                blog_en = await ai.generate_blog_markdown(cfg.blog_model, source)
                _write_text(out_dir / "blog_en.md", blog_en)
                logger.info("blog_generated url=%s chars=%s", url, len(blog_en))

                blog_darija = await ai.translate_to_darija(cfg.darija_model, blog_en)
                _write_text(out_dir / "blog_darija.md", blog_darija)
                logger.info("darija_translated url=%s chars=%s", url, len(blog_darija))

                img_prompt = (
                    "Create an editorial-style illustration for this Moroccan Darija cybersecurity blog post. "
                    "No text in the image. Modern, clean style.\n\n"
                    f"DARJA_POST:\n{blog_darija[:4000]}\n"
                )

                image_bytes, image_caption = await ai.generate_illustration(cfg.image_model, img_prompt, aspect_ratio="16:9")
                if image_bytes:
                    _write_bytes(out_dir / "illustration.png", image_bytes)
                    logger.info("image_generated url=%s bytes=%s", url, len(image_bytes))
                else:
                    logger.warning("image_missing url=%s", url)

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
                k1 = store.put_text(
                    f"{s3_prefix}/source.json",
                    (out_dir / "source.json").read_text(encoding="utf-8"),
                    "application/json",
                )
                k2 = store.put_text(f"{s3_prefix}/blog_en.md", blog_en, "text/markdown; charset=utf-8")
                k3 = store.put_text(f"{s3_prefix}/blog_darija.md", blog_darija, "text/markdown; charset=utf-8")
                k4 = store.put_text(
                    f"{s3_prefix}/meta.json",
                    json.dumps(meta, ensure_ascii=False, indent=2),
                    "application/json",
                )
                if image_bytes:
                    k5 = store.put_bytes(f"{s3_prefix}/illustration.png", image_bytes, "image/png")
                else:
                    k5 = None

                logger.info(
                    "s3_uploaded url=%s keys=%s",
                    url,
                    [k for k in [k1, k2, k3, k4, k5] if k],
                )

                db.mark_completed(conn, url, s3_prefix)
                processed += 1
                logger.info("process_done url=%s elapsed_s=%.3f", url, time.monotonic() - item_t0)

            except Exception as e:
                db.mark_failed(conn, url, str(e))
                logger.exception("process_failed url=%s", url)

        logger.info("agent_done processed=%s", processed)
        return processed

    finally:
        await http.aclose()
        await ai.close()
        conn.close()


if __name__ == "__main__":
    count = asyncio.run(run_once())
    print(count)
