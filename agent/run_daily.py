import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fpdf import FPDF

from agent.ai_client import HackClubAIClient
from agent.config import load_config
from agent.linkedin_templates import LINKEDIN_TEMPLATES_TEXT
from agent.s3_store import S3Store
from agent import db
from agent import thn


logger = logging.getLogger("hn_agent")


class _ColorFormatter(logging.Formatter):
    _RESET = "\x1b[0m"
    _COLORS = {
        "DEBUG": "\x1b[90m",
        "INFO": "\x1b[36m",
        "WARNING": "\x1b[33m",
        "ERROR": "\x1b[31m",
        "CRITICAL": "\x1b[41m\x1b[97m",
    }

    def __init__(self, *, use_color: bool) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)s %(name)s %(message)s")
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        if not self._use_color:
            return super().format(record)

        original_levelname = record.levelname
        try:
            color = self._COLORS.get(original_levelname)
            if color:
                record.levelname = f"{color}{original_levelname}{self._RESET}"
            return super().format(record)
        finally:
            record.levelname = original_levelname


def _should_use_colors() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if (os.environ.get("LOG_COLORS") or "").strip() in {"0", "false", "False", "no", "NO"}:
        return False
    term = (os.environ.get("TERM") or "").lower()
    if term == "dumb":
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _configure_logging() -> None:
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ColorFormatter(use_color=_should_use_colors()))
    logging.basicConfig(level=level, handlers=[handler], force=True)

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


def _extract_txt_codeblocks(text: str) -> List[str]:
    if not text:
        return []
    blocks = re.findall(r"```(?:txt|text)\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    out: List[str] = []
    for b in blocks:
        b = (b or "").strip()
        if not b:
            continue
        out.append(b)
    return out


def _build_blog_url(base_url: str, path_template: str, yyyy_mm_dd: str, slug: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        return ""

    yyyy, mm, dd = (yyyy_mm_dd.split("-") + ["", "", ""])[:3]
    path = (path_template or "/posts/{slug}").strip() or "/posts/{slug}"
    path = path.replace("{slug}", slug)
    path = path.replace("{yyyy}", yyyy).replace("{mm}", mm).replace("{dd}", dd)
    if not path.startswith("/"):
        path = "/" + path
    return base + path


async def run_once() -> int:
    _configure_logging()
    cfg = load_config()
    if not cfg.ai_api_key:
        raise RuntimeError("Missing AI_API_KEY (or HACKCLUB_API_KEY)")
    if not cfg.s3_bucket:
        raise RuntimeError("Missing S3_BUCKET")

    logger.info(
        "agent_start s3_bucket=%s s3_prefix=%s output_dir=%s db_path=%s max_items=%s blog_model=%s darija_model=%s prompts_model=%s image_model=%s",
        cfg.s3_bucket,
        cfg.s3_prefix,
        cfg.output_dir,
        cfg.db_path,
        cfg.max_items,
        cfg.blog_model,
        cfg.darija_model,
        cfg.prompts_model,
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
    linkedin_candidates: List[Dict[str, Any]] = []

    try:
        t0 = time.monotonic()
        logger.info("running fetch_listing")
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

                logger.info("running fetch_source url=%s", url)
                source = await _fetch_source_bundle(http, item)
                _write_text(out_dir / "source.json", json.dumps(source, ensure_ascii=False, indent=2))
                logger.info("source_fetched url=%s", url)

                logger.info("running generate_blog model=%s", cfg.blog_model)
                blog_en = await ai.generate_blog_markdown(cfg.blog_model, source)
                _write_text(out_dir / "blog_en.md", blog_en)
                logger.info("blog_generated url=%s chars=%s", url, len(blog_en))

                logger.info("running translate_darija model=%s", cfg.darija_model)
                blog_darija = await ai.translate_to_darija(cfg.darija_model, blog_en)
                _write_text(out_dir / "blog_darija.md", blog_darija)
                logger.info("darija_translated url=%s chars=%s", url, len(blog_darija))

                blog_url = _build_blog_url(cfg.blog_site_base_url, cfg.blog_site_post_url_template, today, slug) or url
                linkedin_candidates.append(
                    {
                        "title": title or "",
                        "source_url": url,
                        "slug": slug,
                        "blog_url": blog_url,
                        "summary": source.get("description") or item.get("description") or "",
                        "tags": item.get("tags") or "",
                        "out_dir": str(out_dir),
                    }
                )

                logger.info("running generate_manga_prompts model=%s", cfg.prompts_model)
                manga_prompts_raw = await ai.generate_manga_prompts(cfg.prompts_model, blog_darija)
                _write_text(out_dir / "manga_prompts.md", manga_prompts_raw)

                prompts = _extract_txt_codeblocks(manga_prompts_raw)
                if len(prompts) < 4:
                    logger.warning("manga_prompts_incomplete found=%s", len(prompts))
                    logger.info("running generate_manga_prompts_retry model=%s", cfg.prompts_model)
                    manga_prompts_raw = await ai.generate_manga_prompts(cfg.prompts_model, blog_darija)
                    _write_text(out_dir / "manga_prompts_retry.md", manga_prompts_raw)
                    _write_text(out_dir / "manga_prompts.md", manga_prompts_raw)
                    prompts = _extract_txt_codeblocks(manga_prompts_raw)

                if len(prompts) < 4:
                    raise RuntimeError(f"Expected 4 manga prompts, got {len(prompts)}")

                page_images: List[bytes] = []
                page_captions: List[str] = []
                for i, p in enumerate(prompts[:4], start=1):
                    logger.info("running generate_manga_image page=%s model=%s", i, cfg.image_model)
                    img_bytes, caption = await ai.generate_illustration(cfg.image_model, p, aspect_ratio="3:4")
                    if not img_bytes:
                        raise RuntimeError(f"Image generation returned no bytes for page {i}")
                    page_images.append(img_bytes)
                    page_captions.append(caption or "")
                    _write_bytes(out_dir / f"manga_page_{i}.png", img_bytes)
                    logger.info("manga_image_generated page=%s bytes=%s", i, len(img_bytes))

                pdf_path = out_dir / "manga_pages.pdf"
                pdf = FPDF(unit="mm", format="A4")
                pdf.set_margins(0, 0, 0)
                pdf.set_auto_page_break(auto=False)
                for i in range(1, 5):
                    pdf.add_page()
                    img_path = out_dir / f"manga_page_{i}.png"
                    pdf.image(str(img_path), x=0, y=0, w=pdf.w, h=pdf.h)
                pdf.output(str(pdf_path))

                pdf_bytes = pdf_path.read_bytes()
                logger.info("manga_pdf_created bytes=%s", len(pdf_bytes))

                meta = {
                    "source_url": url,
                    "generated_at": int(time.time()),
                    "models": {
                        "blog_model": cfg.blog_model,
                        "darija_model": cfg.darija_model,
                        "prompts_model": cfg.prompts_model,
                        "image_model": cfg.image_model,
                    },
                    "manga_page_captions": page_captions,
                }
                _write_text(out_dir / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))

                s3_prefix = f"{today}/{slug}"
                logger.info("running s3_upload prefix=%s", s3_prefix)
                k1 = store.put_text(
                    f"{s3_prefix}/source.json",
                    (out_dir / "source.json").read_text(encoding="utf-8"),
                    "application/json",
                )
                k2 = store.put_text(f"{s3_prefix}/blog_en.md", blog_en, "text/markdown; charset=utf-8")
                k3 = store.put_text(f"{s3_prefix}/blog_darija.md", blog_darija, "text/markdown; charset=utf-8")
                k_prompts = store.put_text(f"{s3_prefix}/manga_prompts.md", manga_prompts_raw, "text/markdown; charset=utf-8")
                k4 = store.put_text(
                    f"{s3_prefix}/meta.json",
                    json.dumps(meta, ensure_ascii=False, indent=2),
                    "application/json",
                )

                img_keys: List[str] = []
                for i, img_bytes in enumerate(page_images, start=1):
                    img_keys.append(store.put_bytes(f"{s3_prefix}/manga_page_{i}.png", img_bytes, "image/png"))

                k_pdf = store.put_bytes(f"{s3_prefix}/manga_pages.pdf", pdf_bytes, "application/pdf")

                logger.info(
                    "s3_uploaded url=%s keys=%s",
                    url,
                    [k for k in [k1, k2, k3, k_prompts, k4, k_pdf, *img_keys] if k],
                )

                db.mark_completed(conn, url, s3_prefix)
                processed += 1
                logger.info("process_done url=%s elapsed_s=%.3f", url, time.monotonic() - item_t0)

            except Exception as e:
                db.mark_failed(conn, url, str(e))
                logger.exception("process_failed url=%s", url)

        if cfg.linkedin_enable:
            li_dir = base_out / "_linkedin"
            li_json_path = li_dir / "linkedin_draft.json"
            if li_json_path.exists():
                logger.info("skip_linkedin_draft reason=already_exists path=%s", str(li_json_path))
            elif not linkedin_candidates:
                logger.info("skip_linkedin_draft reason=no_candidates")
            else:
                logger.info("running linkedin_pick_best model=%s candidates=%s", cfg.linkedin_model, len(linkedin_candidates))

                ai_candidates = [
                    {
                        "title": c.get("title"),
                        "source_url": c.get("source_url"),
                        "blog_url": c.get("blog_url"),
                        "summary": c.get("summary"),
                        "tags": c.get("tags"),
                    }
                    for c in linkedin_candidates
                ]

                pick = await ai.pick_best_article_for_linkedin(cfg.linkedin_model, ai_candidates)
                try:
                    selected_index = int(pick.get("selected_index", 0))
                except Exception:
                    selected_index = 0
                if selected_index < 0 or selected_index >= len(linkedin_candidates):
                    selected_index = 0
                selected = linkedin_candidates[selected_index]

                sel_dir = Path(str(selected.get("out_dir")))
                blog_darija_full = (sel_dir / "blog_darija.md").read_text(encoding="utf-8")
                blog_en_full = (sel_dir / "blog_en.md").read_text(encoding="utf-8")

                logger.info("running linkedin_pick_template model=%s", cfg.linkedin_model)
                template_pick = await ai.pick_linkedin_template(cfg.linkedin_model, blog_darija_full, LINKEDIN_TEMPLATES_TEXT)
                try:
                    template_number = int(template_pick.get("template_number", 2))
                except Exception:
                    template_number = 2
                if template_number < 1 or template_number > 9:
                    template_number = 2

                logger.info("running linkedin_generate_draft model=%s template=%s", cfg.linkedin_model, template_number)
                draft = await ai.generate_linkedin_draft(
                    model=cfg.linkedin_model,
                    blog_darija=blog_darija_full,
                    blog_en=blog_en_full,
                    template_number=template_number,
                    templates_text=LINKEDIN_TEMPLATES_TEXT,
                    link_url=str(selected.get("blog_url") or selected.get("source_url") or ""),
                    brand=cfg.linkedin_brand,
                )

                post_text = str(draft.get("post_text") or "").strip()
                first_comment = str(draft.get("first_comment") or "").strip()
                if not first_comment:
                    link_url = str(selected.get("blog_url") or selected.get("source_url") or "").strip()
                    first_comment = f"{link_url}\n{cfg.linkedin_brand}".strip()

                hashtags = draft.get("hashtags")
                if not isinstance(hashtags, list):
                    hashtags = []

                li_dir.mkdir(parents=True, exist_ok=True)
                out_json = {
                    "dry_run": bool(cfg.linkedin_dry_run),
                    "generated_at": int(time.time()),
                    "selected_index": selected_index,
                    "selected": {
                        "title": selected.get("title"),
                        "source_url": selected.get("source_url"),
                        "blog_url": selected.get("blog_url"),
                        "slug": selected.get("slug"),
                    },
                    "selection": pick,
                    "template": template_pick,
                    "draft": {
                        "chosen_template_number": draft.get("chosen_template_number", template_number),
                        "post_text": post_text,
                        "first_comment": first_comment,
                        "hashtags": hashtags,
                    },
                }

                _write_text(li_dir / "linkedin_templates.md", LINKEDIN_TEMPLATES_TEXT)
                _write_text(li_dir / "linkedin_draft.json", json.dumps(out_json, ensure_ascii=False, indent=2))

                md = post_text
                if hashtags:
                    md = md + "\n\n" + " ".join([f"#{h.lstrip('#').strip()}" for h in hashtags if isinstance(h, str) and h.strip()])
                md = md + "\n\n---\n\nFIRST COMMENT:\n" + first_comment + "\n"
                _write_text(li_dir / "linkedin_draft.md", md)

                li_prefix = f"{today}/_linkedin"
                store.put_text(f"{li_prefix}/linkedin_templates.md", LINKEDIN_TEMPLATES_TEXT, "text/markdown; charset=utf-8")
                store.put_text(f"{li_prefix}/linkedin_draft.json", json.dumps(out_json, ensure_ascii=False, indent=2), "application/json")
                store.put_text(f"{li_prefix}/linkedin_draft.md", md, "text/markdown; charset=utf-8")
                logger.info("linkedin_draft_done dry_run=%s", cfg.linkedin_dry_run)

        logger.info("agent_done processed=%s", processed)
        return processed

    finally:
        await http.aclose()
        await ai.close()
        conn.close()


if __name__ == "__main__":
    count = asyncio.run(run_once())
    print(count)
