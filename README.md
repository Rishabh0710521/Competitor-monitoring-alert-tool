# Competitor watch

Enter a competitor's domain. The tool crawls its blog and pricing page with its own scraper, remembers what it saw, and alerts you (Slack and/or email) only about what is new, with a target keyword and search volume for each new post.

## Run locally
```bash
pip install -r requirements.txt
cp .env.example .env   # optional; keys can also be pasted in the UI
python -m app.main     # http://localhost:8000
```
Deploy: `docker build -t competitor-watch . && docker run -p 8000:8000 -v data:/data competitor-watch`, or use `render.yaml`. Mount a persistent volume for `/data`, otherwise stored history is lost on restart.

Tests (no internet needed): `ALLOW_PRIVATE=1 python -m unittest tests.test_e2e -v`

## How it works
1. **Scrape** (`scraper.py`, requests + BeautifulSoup): reads robots.txt, sitemaps (incl. sitemap indexes and .gz), RSS/Atom feeds, and blog listing pages with pagination. Three sources so one failing doesn't lose the blog.
2. **Detect new content** (`db.py`, `monitor.py`): every URL is normalised (https, no www, no utm params, no trailing slash) and stored in SQLite. A post is "new" only if its URL isn't stored. Only unseen URLs are fetched, to read title, publish date (JSON-LD, OpenGraph, meta, `<time>`) and meta description.
3. **Enrich** (`enrich.py`): target keyword is derived from title, slug, headings and description. Volume comes from Keywords Everywhere or DataForSEO.
4. **Alert** (`alerts.py`): Slack-compatible webhook and/or SMTP email, listing title, URL, keyword and volume.
5. **Pricing**: the pricing page's price lines are snapshotted; a change produces an added/removed diff in the alert.

## Behaviour on the hard cases
| Case | Result |
|---|---|
| Second run on same domain | Only new URLs flagged. Run 1 is a baseline: all posts stored, the 10 newest are listed and enriched. |
| No blog | `no_blog` with a message explaining what was checked |
| Bot protection (403/429/challenge page) | `blocked`; if sitemap/feed still work, results continue with a warning |
| robots.txt disallows | `blocked_by_robots` (override with `RESPECT_ROBOTS=0` only with permission) |
| Missing / invalid / exhausted API key | Run still succeeds; posts show volume "n/a" with the reason (`invalid_api_key`, `quota_exceeded`, ...) |
| Slack/SMTP failure | Reported per channel in the response, never crashes the run |
| Unreachable domain / bad input | `unreachable` / 400 `invalid` |

## Limits (be upfront in the demo)
- No JavaScript rendering. A blog drawn only client-side with no sitemap/feed won't be found.
- Target keyword is a heuristic, not what the author actually targeted.
- Per run, at most 60 unseen pages are fully fetched and 25 volume lookups are made; the rest are picked up on later runs.
- Private IPs are blocked (SSRF guard); checks are rate limited per IP.
