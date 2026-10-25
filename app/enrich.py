"""Target keyword + estimated search volume.

Keyword: derived from title / slug / headings / meta description (heuristic, no API).
Volume : DataForSEO (Google Ads search volume) or Keywords Everywhere. Failures never crash a run;
         they come back as a status the UI and the alert show ("invalid_api_key", "quota_exceeded", ...).
"""
import os
import re
from urllib.parse import urlparse

import requests

STOP = set("a an the and or of to for in on at by with from your you our is are was be how what why when which who "
           "do does can this that these those it its as vs versus into about without using use we i my not no "
           "guide tips ways ultimate complete definitive step steps beginner beginners".split())
PREFIX = re.compile(r"^(how to|what is|what are|why|when to|the ultimate guide to|ultimate guide to|a complete guide to|"
                    r"complete guide to|a guide to|guide to|the beginner'?s guide to|beginner'?s guide to)\s+")
WEIGHT = {1: 0.6, 2: 1.0, 3: 1.15, 4: 1.0}


def _tokens(s):
    return re.findall(r"[a-z0-9]+(?:['’][a-z]+)?", (s or "").lower())


def derive_keyword(title, url="", description="", headings=""):
    slug = urlparse(url).path.rstrip("/").split("/")[-1].replace("-", " ") if url else ""
    t = re.sub(r"\b(19|20)\d{2}\b", " ", (title or "").lower())
    t = PREFIX.sub("", t.strip())
    head = re.split(r"\s*[:|–—]\s*|\s+-\s+", t)[0]
    if ":" in t and 2 <= len(_tokens(head)) <= 5:
        return " ".join(_tokens(head))
    words = [w for w in _tokens(t) if not w.isdigit()]
    if not words:
        words = _tokens(slug)
    ctx = {"slug": " " + " ".join(_tokens(slug)) + " ", "desc": " " + " ".join(_tokens(description)) + " ",
           "head": " " + " ".join(_tokens(headings)) + " "}
    best, best_score = None, 0
    for n in (4, 3, 2, 1):
        for i in range(len(words) - n + 1):
            g = words[i:i + n]
            if g[0] in STOP or g[-1] in STOP:
                continue
            gs = " " + " ".join(g) + " "
            score = (3 + 2 * ctx["slug"].count(gs) + 1.5 * ctx["desc"].count(gs) + ctx["head"].count(gs)) * WEIGHT[n]
            if score > best_score:
                best, best_score = " ".join(g), score
    return best or " ".join(words[:4]) or "unknown"


# ---------------------------------------------------------------- volume providers
def _dataforseo(keywords, cred):
    if ":" not in cred:
        return {}, "invalid_api_key", "DataForSEO credentials must look like login:password."
    login, pw = cred.split(":", 1)
    url = os.getenv("DFS_URL", "https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live")
    r = requests.post(url, auth=(login, pw), timeout=40,
                      json=[{"keywords": keywords, "location_code": 2840, "language_code": "en"}])
    if r.status_code in (401, 403):
        return {}, "invalid_api_key", "DataForSEO rejected the login/password."
    if r.status_code == 429:
        return {}, "rate_limited", "DataForSEO rate limit hit."
    body = r.json()
    code = body.get("status_code") or 0
    task = (body.get("tasks") or [{}])[0]
    tcode = task.get("status_code") or code
    if code in (40100, 40101, 40104) or tcode in (40100, 40101, 40104):
        return {}, "invalid_api_key", "DataForSEO rejected the login/password."
    if tcode in (40200, 40210):
        return {}, "quota_exceeded", "DataForSEO balance/credits exhausted."
    if tcode == 40202:
        return {}, "rate_limited", "DataForSEO rate limit hit."
    if tcode != 20000:
        return {}, "api_error", f"DataForSEO error {tcode}: {task.get('status_message', '')}"
    out = {}
    for item in task.get("result") or []:
        out[item["keyword"].lower()] = {"volume": item.get("search_volume"), "cpc": item.get("cpc"),
                                        "competition": item.get("competition_index")}
    return out, "ok", ""


def _keywords_everywhere(keywords, key):
    url = os.getenv("KE_URL", "https://api.keywordseverywhere.com/v1/get_keyword_data")
    r = requests.post(url, headers={"Accept": "application/json", "Authorization": f"Bearer {key}"}, timeout=40,
                      data={"country": "us", "currency": "USD", "dataSource": "gkp", "kw[]": keywords})
    if r.status_code in (401, 403):
        return {}, "invalid_api_key", "Keywords Everywhere rejected the API key."
    if r.status_code == 402:
        return {}, "quota_exceeded", "Keywords Everywhere credits exhausted."
    if r.status_code == 429:
        return {}, "rate_limited", "Keywords Everywhere rate limit hit."
    if r.status_code != 200:
        return {}, "api_error", f"Keywords Everywhere returned HTTP {r.status_code}."
    out = {}
    for item in r.json().get("data", []):
        cpc = item.get("cpc") or {}
        out[item["keyword"].lower()] = {"volume": item.get("vol"), "cpc": cpc.get("value") if isinstance(cpc, dict) else cpc,
                                        "competition": item.get("competition")}
    return out, "ok", ""


def lookup_volumes(keywords, provider=None, key=None):
    """Returns (data, status, message, provider_name). Never raises."""
    keywords = sorted({k.lower() for k in keywords if k})
    provider = (provider or "auto").lower()
    dfs_env = f"{os.getenv('DATAFORSEO_LOGIN', '')}:{os.getenv('DATAFORSEO_PASSWORD', '')}"
    if provider == "auto":
        if key:
            provider = "dataforseo" if ":" in key else "keywordseverywhere"
        elif os.getenv("DATAFORSEO_LOGIN"):
            provider, key = "dataforseo", dfs_env
        elif os.getenv("KEYWORDS_EVERYWHERE_API_KEY"):
            provider, key = "keywordseverywhere", os.getenv("KEYWORDS_EVERYWHERE_API_KEY")
        else:
            return {}, "no_api_key", "No keyword API configured. Add a Keywords Everywhere key or DataForSEO login.", "none"
    if not key:
        key = dfs_env if provider == "dataforseo" and os.getenv("DATAFORSEO_LOGIN") else os.getenv("KEYWORDS_EVERYWHERE_API_KEY", "")
    if not key or key == ":":
        return {}, "no_api_key", f"No API key provided for {provider}.", provider
    if not keywords:
        return {}, "ok", "", provider
    try:
        fn = _dataforseo if provider == "dataforseo" else _keywords_everywhere
        data, status, msg = fn(keywords, key)
    except requests.Timeout:
        return {}, "timeout", "The keyword API timed out.", provider
    except (requests.RequestException, ValueError, KeyError) as e:
        return {}, "api_error", f"Keyword API failed: {type(e).__name__}", provider
    return data, status, msg, provider
