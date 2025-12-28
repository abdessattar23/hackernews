import json
import os
import re
import time
import gzip
import urllib.request
from urllib.parse import urljoin, urlparse
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

API_KEY = os.environ.get("API_KEY", "CHANGE_ME")
HN_URL = "https://thehackernews.com/search/label/hacking%20news"
HN_BASE_URL = "https://thehackernews.com"

CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "10"))
MAX_STALE_SECONDS = int(os.environ.get("MAX_STALE_SECONDS", "300"))
CONTENT_CACHE_TTL_SECONDS = int(os.environ.get("CONTENT_CACHE_TTL_SECONDS", "60"))

_opener: Optional[urllib.request.OpenerDirector] = None
_cache_ts: float = 0.0
_cache_items: List[Dict[str, Any]] = []

_content_cache_ts: Dict[str, float] = {}
_content_cache_json: Dict[str, Dict[str, Any]] = {}
_content_cache_html: Dict[str, str] = {}


def _get_opener() -> urllib.request.OpenerDirector:
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener()
    return _opener


def _extract_date(raw: str) -> str:
    text = (raw or "").strip()
    match = re.search(r"([A-Za-z]{3}\s+\d{1,2},\s+\d{4})", text)
    return match.group(1) if match else text


def _parse_news(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    articles = soup.find_all("div", class_="body-post clear")
    news_list: List[Dict[str, Any]] = []

    for article in articles:
        title_el = article.find("h2", class_="home-title")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)

        link_el = title_el.find_parent("a", href=True) or article.find("a", href=True)
        url = link_el["href"] if link_el else None

        img_el = article.find("img")
        image = None
        if img_el:
            image = img_el.get("data-src") or img_el.get("src")

        date_el = article.find("span", class_="h-datetime")
        date_string = _extract_date(date_el.get_text(" ", strip=True) if date_el else "")

        tags_el = article.find("span", class_="h-tags")
        tags = tags_el.get_text(" ", strip=True) if tags_el else None

        desc_el = article.find("div", class_="home-desc")
        description = desc_el.get_text(" ", strip=True) if desc_el else None

        news_list.append(
            {
                "title": title,
                "url": url,
                "image": image,
                "date": date_string,
                "tags": tags,
                "description": description,
            }
        )

    return news_list


def _fetch_text(url: str, timeout_seconds: int = 7) -> str:
    opener = _get_opener()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip",
        },
        method="GET",
    )

    with opener.open(req, timeout=timeout_seconds) as resp:
        status = getattr(resp, "status", None) or 200
        if status < 200 or status >= 300:
            raise RuntimeError(f"HTTP {status}")

        data = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding:
            data = gzip.decompress(data)

        charset = resp.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace")


def _resolve_article_url(article_id: str) -> str:
    raw = (article_id or "").strip()
    if not raw:
        raise ValueError("Missing id")

    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if host == "thehackernews.com" or host.endswith(".thehackernews.com"):
            return raw
        raise ValueError("Only thehackernews.com URLs are allowed")

    if "://" in raw:
        raise ValueError("Invalid id")

    if not raw.startswith("/"):
        raw = "/" + raw

    return urljoin(HN_BASE_URL, raw)


def _dedupe_keep_order(values: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for v in values:
        if not v:
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _parse_article(html: str, url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.find("h1")
    title = title_el.get_text(" ", strip=True) if title_el else None

    content_el = (
        soup.find("div", id=re.compile(r"articlebody", re.I))
        or soup.find("div", class_=re.compile(r"articlebody", re.I))
        or soup.find("div", class_=re.compile(r"post-body|entry-content", re.I))
        or soup.find("article")
    )

    content_html = content_el.decode_contents() if content_el else None
    text = content_el.get_text(" ", strip=True) if content_el else None

    images: List[str] = []
    links: List[str] = []
    if content_el:
        for img in content_el.find_all("img"):
            src = img.get("data-src") or img.get("src")
            if src:
                images.append(src)
        for a in content_el.find_all("a", href=True):
            links.append(a.get("href"))

    images = _dedupe_keep_order(images)
    links = _dedupe_keep_order(links)

    return {
        "url": url,
        "title": title,
        "content_html": content_html,
        "text": text,
        "images": images,
        "links": links,
    }


def _get_article_cached(url: str, force_refresh: bool) -> Tuple[Dict[str, Any], str]:
    now = time.monotonic()
    ts = _content_cache_ts.get(url, 0.0)
    age = now - ts

    if not force_refresh and url in _content_cache_json and age <= CONTENT_CACHE_TTL_SECONDS:
        return _content_cache_json[url], _content_cache_html.get(url, "")

    html = _fetch_text(url)
    parsed = _parse_article(html, url)

    _content_cache_ts[url] = time.monotonic()
    _content_cache_json[url] = parsed
    _content_cache_html[url] = html
    return parsed, html


def _scrape_now() -> List[Dict[str, Any]]:
    html = _fetch_text(HN_URL)
    return _parse_news(html)


def _get_cached_news(force_refresh: bool) -> List[Dict[str, Any]]:
    global _cache_ts, _cache_items

    now = time.monotonic()
    age = now - _cache_ts
    has_cache = len(_cache_items) > 0
    fresh = has_cache and age <= CACHE_TTL_SECONDS
    stale_ok = has_cache and age <= MAX_STALE_SECONDS

    if not force_refresh:
        if fresh or stale_ok:
            return list(_cache_items)

    items = _scrape_now()
    _cache_items = items
    _cache_ts = time.monotonic()
    return list(_cache_items)


def _normalize_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not headers:
        return out
    for k, v in headers.items():
        if k is None:
            continue
        out[str(k).lower()] = "" if v is None else str(v)
    return out


def _parse_query(event: Dict[str, Any]) -> Dict[str, str]:
    qs = event.get("queryStringParameters")
    if isinstance(qs, dict) and qs:
        return {str(k): "" if v is None else str(v) for k, v in qs.items()}

    raw_qs = event.get("rawQueryString")
    if isinstance(raw_qs, str) and raw_qs:
        pairs: Dict[str, str] = {}
        for part in raw_qs.split("&"):
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
            else:
                k, v = part, ""
            pairs[k] = v
        return pairs

    return {}


def _get_method_path(event: Dict[str, Any]) -> Tuple[str, str]:
    method = "GET"
    path = "/"

    rc = event.get("requestContext")
    if isinstance(rc, dict):
        http = rc.get("http")
        if isinstance(http, dict):
            method = http.get("method") or method
            path = http.get("path") or event.get("rawPath") or path

    method = event.get("httpMethod") or method
    path = event.get("path") or event.get("rawPath") or path

    return str(method).upper(), str(path)


def _json(status: int, payload: Any) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "isBase64Encoded": False,
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _html(status: int, body: str) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
        },
        "isBase64Encoded": False,
        "body": body,
    }


def _unauthorized() -> Dict[str, Any]:
    return _json(401, {"detail": "Invalid API key"})


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    method, path = _get_method_path(event)
    headers = _normalize_headers(event.get("headers"))
    query = _parse_query(event)

    if method != "GET":
        return _json(405, {"detail": "Method not allowed"})

    if path.endswith("/health") or path == "/health":
        return _json(200, {"status": "ok"})

    api_key = headers.get("x-api-key")
    if api_key != API_KEY:
        return _unauthorized()

    refresh = (query.get("refresh", "false").lower() == "true")

    if path.endswith("/latest") or path == "/latest":
        items = _get_cached_news(force_refresh=refresh)
        return _json(200, (items[0] if items else None))

    if path.endswith("/news") or path == "/news":
        try:
            limit = int(query.get("limit", "20"))
        except ValueError:
            limit = 20
        if limit < 1:
            limit = 1
        if limit > 100:
            limit = 100
        items = _get_cached_news(force_refresh=refresh)
        return _json(200, items[:limit])

    if path.endswith("/content") or path == "/content":
        article_id = query.get("id")
        if not article_id:
            return _json(400, {"detail": "Missing query parameter: id"})

        fmt = (query.get("format") or "json").lower()
        raw = (query.get("raw", "false").lower() == "true")
        try:
            url = _resolve_article_url(article_id)
            parsed, full_html = _get_article_cached(url=url, force_refresh=refresh)
        except ValueError as e:
            return _json(400, {"detail": str(e)})
        except Exception:
            return _json(502, {"detail": "Failed to fetch article"})

        if fmt == "html":
            if raw:
                return _html(200, full_html)
            return _html(200, parsed.get("content_html") or "")

        return _json(200, parsed)

    return _json(404, {"detail": "Not found"})
