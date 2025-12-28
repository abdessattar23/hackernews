# The Hacker News Scraper API (AWS Lambda Function URL)

## Base URL

All endpoints are relative to your Lambda Function URL:

- `https://v75n4oaduruhvyoceiqukdvbti0pwskb.lambda-url.eu-north-1.on.aws`

Examples below will use this base URL.

---

## Authentication

### API Key

Most endpoints require a static API key passed via header:

- Header: `X-API-Key: <key>`

Example:

```bash
curl -H "X-API-Key: CHANGE_ME" \
  https://v75n4oaduruhvyoceiqukdvbti0pwskb.lambda-url.eu-north-1.on.aws/latest
```

### Endpoints that do NOT require auth

- `GET /health`

---

## Endpoints

### 1) Health Check

#### `GET /health`

- **Auth:** not required
- **Purpose:** simple uptime check

Request:

```bash
curl https://v75n4oaduruhvyoceiqukdvbti0pwskb.lambda-url.eu-north-1.on.aws/health
```

Response (200):

```json
{"status":"ok"}
```

---

### 2) Latest Article

#### `GET /latest`

- **Auth:** required (`X-API-Key`)
- **Purpose:** returns the newest article summary from TheHackerNews listing page

Query parameters:

- `refresh` (optional, boolean, default: `false`)
  - `true` forces a live fetch of the listing page, bypassing in-memory cache.

Request:

```bash
curl -H "X-API-Key: CHANGE_ME" \
  https://v75n4oaduruhvyoceiqukdvbti0pwskb.lambda-url.eu-north-1.on.aws/latest
```

Response (200):

```json
{
  "title": "...",
  "url": "https://thehackernews.com/....html",
  "image": "https://...jpg",
  "date": "Sep 20, 2025",
  "tags": "Software Security",
  "description": "..."
}
```

Errors:

- `401` if API key is missing/invalid:

```json
{"detail":"Invalid API key"}
```

---

### 3) Latest News List

#### `GET /news`

- **Auth:** required (`X-API-Key`)
- **Purpose:** returns a list of latest article summaries from TheHackerNews listing page

Query parameters:

- `limit` (optional, int, default: `20`)
- `refresh` (optional, boolean, default: `false`)

Request:

```bash
curl -H "X-API-Key: CHANGE_ME" \
  "https://v75n4oaduruhvyoceiqukdvbti0pwskb.lambda-url.eu-north-1.on.aws/news?limit=5"
```

Response (200):

```json
[
  {
    "title": "...",
    "url": "https://thehackernews.com/....html",
    "image": "https://...jpg",
    "date": "...",
    "tags": "...",
    "description": "..."
  }
]
```

Constraints:

- `limit` is clamped to `1..100`.

Errors:

- `401` if API key is missing/invalid

---

### 4) Full Article Content

#### `GET /content?id=...`

- **Auth:** required (`X-API-Key`)
- **Purpose:** fetches and returns the full content for a specific TheHackerNews article

Query parameters:

- `id` (required)
  - Accepts either:
    - A path, e.g. `/2025/12/new-mongodb-flaw-lets-unauthenticated.html`
    - Or a full URL, e.g. `https://thehackernews.com/2025/12/...html`
  - **Only** `thehackernews.com` URLs are allowed.

- `format` (optional, `json` or `html`, default: `json`)
- `raw` (optional, boolean, default: `false`)
  - Only applies when `format=html`.
  - `raw=true` returns the **full page HTML**.
  - `raw=false` returns only the article body HTML.

- `refresh` (optional, boolean, default: `false`)

#### JSON mode (default)

Request:

```bash
curl -H "X-API-Key: CHANGE_ME" \
  "https://v75n4oaduruhvyoceiqukdvbti0pwskb.lambda-url.eu-north-1.on.aws/content?id=/2025/12/new-mongodb-flaw-lets-unauthenticated.html"
```

Response (200):

```json
{
  "url": "https://thehackernews.com/2025/12/new-mongodb-flaw-lets-unauthenticated.html",
  "title": "...",
  "content_html": "<div>...article body html...</div>",
  "text": "...plain text extracted from article body...",
  "images": ["https://..."],
  "links": ["https://...", "..."]
}
```

#### HTML mode (article body)

Request:

```bash
curl -H "X-API-Key: CHANGE_ME" \
  "https://v75n4oaduruhvyoceiqukdvbti0pwskb.lambda-url.eu-north-1.on.aws/content?id=/2025/12/new-mongodb-flaw-lets-unauthenticated.html&format=html"
```

Response (200):

- `Content-Type: text/html; charset=utf-8`
- Body is the article HTML (main content only)

#### HTML mode (raw full page)

Request:

```bash
curl -H "X-API-Key: CHANGE_ME" \
  "https://v75n4oaduruhvyoceiqukdvbti0pwskb.lambda-url.eu-north-1.on.aws/content?id=/2025/12/new-mongodb-flaw-lets-unauthenticated.html&format=html&raw=true"
```

Errors:

- `400` if `id` is missing:

```json
{"detail":"Missing query parameter: id"}
```

- `400` if the URL is not `thehackernews.com`:

```json
{"detail":"Only thehackernews.com URLs are allowed"}
```

- `502` if the article fetch fails:

```json
{"detail":"Failed to fetch article"}
```

---

## Response Codes (Summary)

- `200` OK
- `400` Bad Request (missing/invalid parameters)
- `401` Unauthorized (missing/invalid API key)
- `404` Not Found (unknown route)
- `405` Method Not Allowed (only GET supported)
- `502` Bad Gateway (upstream fetch/parsing failed)

---

## Constraints and Operational Notes

### Allowed Methods

- Only **GET** is supported.

### Host Restriction

- `/content` only allows `thehackernews.com` links/paths.

### Caching

This Lambda uses in-memory caches inside the Lambda runtime container:

- Listing cache (used by `/latest` and `/news`)
  - TTL: `CACHE_TTL_SECONDS` (default: `10`)
  - Max staleness: `MAX_STALE_SECONDS` (default: `300`)

- Article cache (used by `/content`)
  - TTL: `CONTENT_CACHE_TTL_SECONDS` (default: `60`)

Notes:

- Cache is **per Lambda instance**.
- Cache is lost on cold starts.
- Use `refresh=true` to bypass cache.

### Performance

- Cold starts add latency.
- Uncached requests depend on:
  - network latency to TheHackerNews
  - HTML parsing time

For improved speed:

- Increase Lambda memory (CPU scales with memory).

### Payload Size

- Returning raw HTML (`format=html&raw=true`) can be large.
- Lambda Function URL / API Gateway have response size limits; if a page is too large, the request may fail.

---

## Security Notes

- Keep your API key secret.
- Prefer setting `API_KEY` as a Lambda Environment Variable instead of hardcoding.
