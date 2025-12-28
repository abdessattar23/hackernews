import re
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

HN_LISTING_URL = "https://thehackernews.com/"


def _extract_date(raw: str) -> str:
    text = (raw or "").strip()
    match = re.search(r"([A-Za-z]{3}\s+\d{1,2},\s+\d{4})", text)
    return match.group(1) if match else text


async def fetch_listing_html(client: httpx.AsyncClient) -> str:
    r = await client.get(HN_LISTING_URL)
    r.raise_for_status()
    return r.text


def parse_listing(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    articles = soup.find_all("div", class_="body-post clear")
    out: List[Dict[str, Any]] = []

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

        if not url:
            continue

        out.append(
            {
                "title": title,
                "url": url,
                "image": image,
                "date": date_string,
                "tags": tags,
                "description": description,
            }
        )

    return out


async def fetch_article_html(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url)
    r.raise_for_status()
    return r.text


def parse_article(html: str, url: str) -> Dict[str, Any]:
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

    def _dedupe(values: List[str]) -> List[str]:
        seen: set[str] = set()
        out: List[str] = []
        for v in values:
            if not v or v in seen:
                continue
            seen.add(v)
            out.append(v)
        return out

    return {
        "url": url,
        "title": title,
        "content_html": content_html,
        "text": text,
        "images": _dedupe(images),
        "links": _dedupe(links),
    }


def pick_candidates(listing: List[Dict[str, Any]], max_items: int) -> List[Dict[str, Any]]:
    if max_items <= 0:
        return []
    return listing[:max_items]
