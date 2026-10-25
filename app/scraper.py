"""Our own scraper. No third-party scraping API.

Discovery uses three independent sources so one failure doesn't lose the blog:
  1. sitemaps (robots.txt + common paths)   2. RSS/Atom feeds   3. blog listing pages (with pagination)
Each unseen URL is then fetched once to read title, publish date and meta description.
"""
import gzip
import html as htmllib
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import urllib.robotparser
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

UA = os.getenv("SCRAPER_UA", "Mozilla/5.0 (compatible; CompetitorWatch/1.0; +https://github.com/your-name/competitor-watch)")
ROBOT_TOKEN = "CompetitorWatch"
MAX_BYTES = 3_000_000
MAX_LISTING_PAGES = int(os.getenv("MAX_LISTING_PAGES", "5"))


def _flag(name, default):
    return os.getenv(name, default) == "1"


class SiteError(Exception):
    """kind: invalid | unreachable | blocked | blocked_by_robots | no_blog"""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------- helpers
def check_public(url):
    """SSRF guard: only fetch public IPs (also used for webhook URLs)."""
    if _flag("ALLOW_PRIVATE", "0"):
        return
    host = urlparse(url).hostname
    if not host:
        raise SiteError("invalid", "Invalid URL")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise SiteError("unreachable", f"DNS lookup failed for {host}")
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise SiteError("invalid", f"{host} resolves to a private address")


def normalize_domain(raw):
    raw = (raw or "").strip().lower()
    if not raw:
        raise ValueError("Enter a domain, for example ahrefs.com")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    host = (p.hostname or "").removeprefix("www.")
    local_ok = _flag("ALLOW_PRIVATE", "0") and (host == "localhost" or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host))
    if not local_ok and not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}", host):
        raise ValueError(f"\"{raw}\" doesn't look like a domain. Use a format like example.com")
    return host + (f":{p.port}" if p.port else "")


TRACK = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|source$)")


def canonical(url):
    """One stable key per page so re-runs never see the same post as new."""
    p = urlparse(url)
    host = (p.hostname or "").lower().removeprefix("www.")
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if not TRACK.match(k.lower())])
    return f"https://{host}{port}{path}" + (f"?{q}" if q else "")


def parse_date(s):
    if not s:
        return None
    try:
        d = dateparser.parse(str(s).strip())
    except (ValueError, OverflowError, TypeError):
        return None
    if d.tzinfo:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    if d.year < 1995 or d > datetime.utcnow() + timedelta(days=2):
        return None
    return d.date().isoformat()


def slug_title(url):
    seg = [s for s in urlparse(url).path.split("/") if s]
    return re.sub(r"[-_]+", " ", seg[-1] if seg else url).strip().title()


BLOG_SEGS = {"blog", "blogs", "article", "articles", "post", "posts", "resources", "resource", "news", "insights",
             "insight", "learn", "guides", "guide", "stories", "story", "library", "knowledge", "magazine",
             "journal", "updates", "press", "newsroom", "academy"}
SKIP_SEGS = {"tag", "tags", "category", "categories", "author", "authors", "page", "topic", "topics", "feed",
             "wp-json", "wp-content", "wp-admin", "search", "archive", "archives", "amp", "cdn-cgi", "login",
             "signup", "pricing", "contact", "about", "careers", "jobs", "cart", "checkout"}
STATIC = re.compile(r"\.(jpe?g|png|gif|svg|webp|pdf|zip|css|js|ico|xml|json|mp4|mp3|woff2?)$", re.I)
LISTING_PATHS = ["/blog", "/blogs", "/resources", "/articles", "/news", "/insights", "/learn", "/resources/blog",
                 "/library", "/stories", "/journal", "/magazine"]
LISTING_WORDS = {"blog", "resources", "articles", "news", "insights", "learn", "library", "stories", "journal", "magazine"}


def is_post_url(url):
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    if not segs or STATIC.search(p.path) or any(s.lower() in SKIP_SEGS for s in segs):
        return False
    if re.search(r"/20\d{2}/\d{1,2}/", p.path):  # WordPress date permalinks
        return True
    for i, s in enumerate(segs):
        if s.lower() in BLOG_SEGS and i < len(segs) - 1:
            return len(segs[-1]) >= 6
    if (p.hostname or "").startswith(("blog.", "resources.", "news.", "learn.", "insights.")):
        return len(segs[-1]) >= 6
    return False


def looks_like_slug(url):
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    return bool(segs) and segs[-1].count("-") >= 2 and not STATIC.search(p.path) \
        and not any(s.lower() in SKIP_SEGS for s in segs)


def looks_blocked(r):
    if r.status_code in (401, 403, 429, 451, 503, 999):
        return True
    if r.status_code == 200 and len(r.content) < 30000:
        t = r.text[:6000].lower()
        return any(m in t for m in ("just a moment...", "cf-chl", "attention required! | cloudflare",
                                    "verify you are human", "access denied", "px-captcha", "are you a robot"))
    return False


# ---------------------------------------------------------------- page extraction
ARTICLE_TYPES = {"article", "blogposting", "newsarticle", "techarticle", "report", "scholarlyarticle"}


def _meta(soup, **attrs):
    tag = soup.find("meta", attrs=attrs)
    return (tag.get("content") or "").strip() if tag else ""


def _jsonld_articles(soup):
    found = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            types = node.get("@type")
            types = [types] if isinstance(types, str) else (types or [])
            if any(str(t).lower() in ARTICLE_TYPES for t in types):
                found.append(node)
            for k in ("@graph", "mainEntity", "mainEntityOfPage"):
                if k in node:
                    walk(node[k])

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(s.string or s.get_text() or ""))
        except (ValueError, TypeError):
            continue
    return found


def unescape(t):
    """Some sites double-encode entities (&amp;nbsp;), so decode twice."""
    for _ in range(2):
        t = htmllib.unescape(t or "")
    return re.sub(r"\s+", " ", t.replace("\xa0", " ")).strip()


def clean_title(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    parts = re.split(r"\s+[|–—\-·•]\s+", t)
    return parts[0] if len(parts) > 1 and len(parts[0]) > 12 else t


def extract_post(html):
    soup = BeautifulSoup(html, "lxml")
    ld = _jsonld_articles(soup)
    ld0 = ld[0] if ld else {}
    h1 = soup.find("h1")
    h1_text = h1.get_text(" ", strip=True) if h1 else ""
    title = (ld0.get("headline") if isinstance(ld0.get("headline"), str) else "") \
        or _meta(soup, property="og:title") or h1_text \
        or clean_title(soup.title.get_text() if soup.title else "")
    published = None
    for cand in (ld0.get("datePublished"), _meta(soup, property="article:published_time"),
                 _meta(soup, itemprop="datePublished"), _meta(soup, name="date"), _meta(soup, name="publish-date"),
                 _meta(soup, name="pubdate"), _meta(soup, name="DC.date.issued"), _meta(soup, name="dcterms.created")):
        published = parse_date(cand)
        if published:
            break
    if not published:
        t = (soup.find("article") or soup).find("time")
        if t:
            published = parse_date(t.get("datetime") or t.get_text())
    desc = _meta(soup, name="description") or _meta(soup, property="og:description") \
        or (ld0.get("description") if isinstance(ld0.get("description"), str) else "")
    og_type = _meta(soup, property="og:type").lower()
    return {
        "title": unescape(title),
        "published": published,
        "description": unescape(desc)[:500],
        "headings": [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])][:12],
        "is_article": bool(ld) or "article" in og_type or bool(soup.find("article") and published),
    }


def _text(tag):
    return tag.get_text(" ", strip=True) if tag else ""


def parse_feed(xml_text):
    soup = BeautifulSoup(xml_text, "xml")
    out = []
    for it in soup.find_all(["item", "entry"]):
        link = None
        for l in it.find_all("link"):
            link = l.get("href") or l.get_text(strip=True)
            if link and l.get("rel") in (None, ["alternate"]):
                break
        if not link:
            continue
        pick = lambda names: it.find(lambda t: t.name.split(":")[-1] in names)
        desc = BeautifulSoup(_text(pick({"description", "summary"})), "html.parser").get_text(" ", strip=True)
        out.append({"url": link, "title": unescape(_text(it.find("title"))),
                    "published": parse_date(_text(pick({"pubDate", "published", "updated", "date"}))),
                    "description": desc[:300]})
    return out


PRICE_RE = re.compile(r"(?:[$€£₹¥]\s?\d[\d,]*(?:\.\d+)?|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|INR)\b|\bRs\.?\s?\d[\d,]*)")


# ---------------------------------------------------------------- scraper
class Scraper:
    def __init__(self, domain, time_budget=int(os.getenv("RUN_TIME_BUDGET", "100"))):
        self.domain = domain
        self.hostname = domain.split(":")[0]
        self.deadline = time.time() + time_budget
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        self.base = None
        self.home = None
        self.home_soup = None
        self.robots = None
        self.robots_sitemaps = []
        self.blocked_hits = 0
        self.timed_out = False
        self.last_error = ""
        self.request_count = 0

    # -- networking
    def same_site(self, url):
        h = (urlparse(url).hostname or "").removeprefix("www.")
        return h == self.hostname or h.endswith("." + self.hostname)

    def fetchable(self, url):
        """Canonical keys use https/no-www; fetch with the scheme+host that actually worked."""
        p = urlparse(url)
        base = urlparse(self.base)
        host = (p.hostname or "")
        if host == self.hostname:
            return p._replace(scheme=base.scheme, netloc=base.netloc).geturl()
        return p._replace(scheme=base.scheme).geturl()

    def allowed(self, url):
        return not (RESPECT() and self.robots and not self.robots.can_fetch(ROBOT_TOKEN, url))

    def get(self, url, robots=True, timeout=12):
        if time.time() > self.deadline:
            self.timed_out = True
            return None
        if robots and not self.allowed(url):
            return None
        try:
            check_public(url)
            self.request_count += 1
            r = self.s.get(url, timeout=timeout, stream=True, allow_redirects=True)
            check_public(r.url)
            body = b""
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) > MAX_BYTES:
                    break
            r._content, r._content_consumed = body, True
            r.close()
        except SiteError as e:
            self.last_error = str(e)
            return None
        except requests.RequestException as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
            return None
        if "charset" not in r.headers.get("content-type", "").lower():
            r.encoding = "utf-8"
        if looks_blocked(r):
            self.blocked_hits += 1
            self.last_error = f"HTTP {r.status_code} (bot protection)"
            return None
        if r.status_code >= 400:
            self.last_error = f"HTTP {r.status_code}"
            r.is_gone = r.status_code in (404, 410)
            return None
        return r

    def connect(self):
        n = self.domain
        cands = [("http", n), ("https", n)] if ":" in n else [("https", n), ("https", "www." + n), ("http", n)]
        blocked_base = None
        for scheme, host in cands:
            before = self.blocked_hits
            self.base = f"{scheme}://{host}"
            r = self.get(self.base + "/", robots=False)
            if r is not None:
                self.home, self.home_soup = r, BeautifulSoup(r.text, "lxml")
                break
            if self.blocked_hits > before and not blocked_base:
                blocked_base = self.base
        else:
            if not blocked_base:
                raise SiteError("unreachable", f"Couldn't reach {n} ({self.last_error or 'no response'}). "
                                               "Check the spelling or try again later.")
            self.base = blocked_base
        rb = self.get(self.base + "/robots.txt", robots=False)
        if rb is not None and "<html" not in rb.text[:500].lower():
            self.robots = urllib.robotparser.RobotFileParser()
            self.robots.parse(rb.text.splitlines())
            self.robots_sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", rb.text)
            if RESPECT() and not self.robots.can_fetch(ROBOT_TOKEN, self.base + "/"):
                raise SiteError("blocked_by_robots", f"{n}'s robots.txt disallows crawling. "
                                                     "Set RESPECT_ROBOTS=0 only if you have permission.")

    # -- discovery
    def discover(self):
        seeds = {}

        def add(url, source, strict=True, **kw):
            url = urljoin(self.base + "/", url.strip()).split("#")[0]
            if not url.startswith("http") or not self.same_site(url):
                return False
            if not (is_post_url(url) or (not strict and looks_like_slug(url))):
                return False
            key = canonical(url)
            cur = seeds.setdefault(key, {"source": source})
            for k, v in kw.items():
                if v and not cur.get(k):
                    cur[k] = v
            return True

        self._feeds(add)
        self._sitemaps(add)
        self._listings(seeds, add)
        return seeds

    def _feeds(self, add):
        urls = []
        if self.home_soup:
            for l in self.home_soup.find_all("link"):
                if re.search(r"rss|atom", l.get("type", ""), re.I) and l.get("href"):
                    urls.append(urljoin(self.base + "/", l["href"]))
        urls += [self.base + p for p in ("/feed", "/rss.xml", "/atom.xml", "/blog/feed", "/blog/rss.xml",
                                         "/blog/feed.xml", "/index.xml", "/feed.xml")]
        done = 0
        for u in dict.fromkeys(urls):
            r = self.get(u)
            if r is None or "<html" in r.text[:300].lower():
                continue
            for item in parse_feed(r.text):
                add(item["url"], "feed", title=item["title"], published=item["published"],
                    description=item["description"])
            done += 1
            if done >= 2:
                break

    def _sitemaps(self, add, depth=0, urls=None, budget=None):
        if urls is None:
            urls = self.robots_sitemaps or [self.base + p for p in (
                "/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap-index.xml",
                "/post-sitemap.xml", "/blog-sitemap.xml")]
            budget = [14]
        for u in urls:
            if budget[0] <= 0:
                return
            budget[0] -= 1
            r = self.get(u, robots=False)
            if r is None:
                continue
            data = r.content
            if u.endswith(".gz") or data[:2] == b"\x1f\x8b":
                try:
                    data = gzip.decompress(data)
                except OSError:
                    continue
            soup = BeautifulSoup(data, "xml")
            if soup.find("sitemapindex") and depth < 2:
                kids = [s.loc.get_text(strip=True) for s in soup.find_all("sitemap") if s.loc]
                kids.sort(key=lambda k: 0 if re.search(r"post|blog|article|news|resource|insight|learn", k, re.I) else 1)
                self._sitemaps(add, depth + 1, kids[:10], budget)
            else:
                for e in soup.find_all("url"):
                    if e.loc:
                        add(e.loc.get_text(strip=True), "sitemap", published=parse_date(_text(e.find("lastmod"))))
            if depth == 0 and self.robots_sitemaps == [] and soup.find(["urlset", "sitemapindex"]):
                return  # first working default sitemap is enough

    def _listings(self, seeds, add):
        roots = []
        if self.home_soup:
            for a in self.home_soup.find_all("a", href=True):
                url = urljoin(self.base + "/", a["href"]).split("#")[0]
                segs = [s for s in urlparse(url).path.split("/") if s]
                if self.same_site(url) and ((len(segs) == 1 and segs[0].lower() in BLOG_SEGS)
                                            or (len(segs) <= 2 and a.get_text(strip=True).lower() in LISTING_WORDS)):
                    roots.append(url)
        prefixes = Counter()
        for k in seeds:
            segs = [s for s in urlparse(k).path.split("/") if s]
            for i, s in enumerate(segs[:-1]):
                if s.lower() in BLOG_SEGS:
                    prefixes["/" + "/".join(segs[:i + 1])] += 1
                    break
        roots += [self.base + p for p, _ in prefixes.most_common(2)]
        if not roots:
            roots = [self.base + p for p in LISTING_PATHS]
        seen_roots = set()
        crawled = 0
        for root in roots:
            c = canonical(root)
            if c in seen_roots or crawled >= 4 or (not seeds and not self._probe_ok(root, crawled)):
                continue
            seen_roots.add(c)
            if self._crawl_listing(root, add):
                crawled += 1

    def _probe_ok(self, root, crawled):
        return crawled < 3

    def _crawl_listing(self, root, add):
        url, hit = root, False
        visited = set()
        for page in range(1, MAX_LISTING_PAGES + 1):
            if url in visited:
                break
            visited.add(url)
            r = self.get(url)
            if r is None:
                break
            soup = BeautifulSoup(r.text, "lxml")
            links = 0
            for a in soup.find_all("a", href=True):
                card = a.find_parent(["article", "li"]) or a.find_parent(class_=re.compile("post|card|entry|article|blog", re.I))
                t = (card.find("time") if card else None)
                text = a.get_text(" ", strip=True)
                if add(urljoin(url, a["href"]), "listing", strict=False if card else True,
                       title=text if len(text) > 15 else None,
                       published=parse_date(t.get("datetime") or t.get_text()) if t else None):
                    links += 1
            hit = hit or links > 0
            nxt = self._next_page(soup, url, page, links)
            if not nxt:
                break
            url = nxt
        return hit

    def _next_page(self, soup, url, page, links):
        tag = soup.find("link", rel="next") or soup.find("a", rel="next")
        if tag and tag.get("href"):
            return urljoin(url, tag["href"])
        for a in soup.find_all("a", href=True):
            label = (a.get_text(" ", strip=True) + " " + (a.get("aria-label") or "")).lower()
            if re.search(r"\b(next|older)\b|^[›»]", label.strip()) and not a["href"].startswith("#"):
                return urljoin(url, a["href"])
        if page == 1 and links >= 5:  # conventional fallback
            return url.rstrip("/") + "/page/2"
        return None

    # -- per-post detail
    def fetch_post(self, url, seed):
        r = self.get(self.fetchable(url))
        if r is None:
            if getattr(self, "last_error", "").startswith(("HTTP 404", "HTTP 410")):
                return None
            return self.shallow(url, seed)  # blocked / timeout: keep what we know, hydrate later
        d = extract_post(r.text)
        if not d["is_article"] and seed.get("source") == "listing" and not d["published"]:
            return None
        if not d["is_article"] and not d["published"] and not seed.get("published"):
            return None
        return {"title": d["title"] or seed.get("title") or slug_title(url),
                "published": d["published"] or seed.get("published"),
                "description": d["description"] or seed.get("description") or "",
                "headings": " | ".join(d["headings"]), "detail": 1, "source": seed.get("source", "")}

    def shallow(self, url, seed):
        return {"title": seed.get("title") or slug_title(url), "published": seed.get("published"),
                "description": seed.get("description") or "", "headings": "", "detail": 0,
                "source": seed.get("source", "")}

    # -- pricing
    def pricing(self):
        cands = []
        if self.home_soup:
            for a in self.home_soup.find_all("a", href=True):
                if re.search(r"pricing|plans", a["href"] + " " + a.get_text(), re.I) and self.same_site(urljoin(self.base + "/", a["href"])):
                    cands.append(urljoin(self.base + "/", a["href"]).split("#")[0])
        cands += [self.base + "/pricing", self.base + "/plans"]
        for u in list(dict.fromkeys(cands))[:4]:
            r = self.get(u)
            if r is None:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            for t in soup(["script", "style", "noscript", "svg"]):
                t.decompose()
            lines = [re.sub(r"\s+", " ", l).strip() for l in soup.get_text("\n").split("\n")]
            lines = [l for l in lines if l]
            found, seen = [], set()
            for i, l in enumerate(lines):
                if PRICE_RE.search(l) and len(l) < 160:
                    label = lines[i - 1][:40] if i and not PRICE_RE.search(lines[i - 1]) else ""
                    entry = f"{label} | {l}" if label else l
                    if entry not in seen:
                        seen.add(entry)
                        found.append(entry)
            found = found[:60]
            return {"url": canonical(u), "lines": found,
                    "fingerprint": hashlib.sha1("\n".join(found).encode()).hexdigest()}
        return None


def RESPECT():
    return _flag("RESPECT_ROBOTS", "1")