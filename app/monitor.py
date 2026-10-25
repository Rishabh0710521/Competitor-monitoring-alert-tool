"""One check = connect -> discover -> diff against DB -> fetch only unseen URLs -> enrich -> pricing diff -> alert."""
import os

from . import alerts, db, enrich
from .scraper import Scraper, SiteError, normalize_domain

MAX_FETCH = int(os.getenv("MAX_FETCH", "60"))          # unseen pages fully fetched per run
BACKFILL = int(os.getenv("BACKFILL", "15"))            # older shallow records hydrated per run
BASELINE_ENRICH = int(os.getenv("BASELINE_ENRICH", "10"))
ENRICH_CAP = int(os.getenv("ENRICH_CAP", "25"))        # protects API credits


def _sort_key(p):
    return p.get("published") or ""


def _fail(res, run_id, kind, message):
    db.finish_run(run_id, kind, message)
    res.update(ok=False, status=kind, message=message)
    return res


def _pricing_diff(domain, pr):
    if not pr or not pr["lines"]:
        return None
    prev = db.latest_pricing(domain)
    if prev and prev["fingerprint"] == pr["fingerprint"]:
        return None
    db.add_pricing(domain, pr["url"], pr["fingerprint"], pr["lines"])
    if not prev:
        return {"first_snapshot": True, "url": pr["url"], "added": [], "removed": [], "changed": False}
    old, new = set(prev["lines"]), set(pr["lines"])
    return {"first_snapshot": False, "changed": True, "url": pr["url"],
            "added": sorted(new - old), "removed": sorted(old - new)}


def run_check(raw_domain, slack_webhook=None, email_to=None, kw_provider=None, kw_key=None, notify=True):
    domain = normalize_domain(raw_domain)  # ValueError -> caller returns 400
    db.init()
    site = db.get_site(domain)
    first_run, prev_checked = site is None, (site or {}).get("last_checked")
    run_id = db.new_run(domain, first_run)
    res = {"ok": True, "status": "ok", "domain": domain, "first_run": first_run, "notes": []}
    sc = Scraper(domain)
    try:
        sc.connect()
        seeds = sc.discover()
    except SiteError as e:
        return _fail(res, run_id, e.kind, str(e))
    if not seeds:
        if sc.blocked_hits:
            return _fail(res, run_id, "blocked", f"{domain} blocked our requests ({sc.last_error}). It uses bot "
                         "protection, so no posts could be read. Try again later or from a different network.")
        return _fail(res, run_id, "no_blog", f"No blog found on {domain}. We checked sitemaps, RSS feeds and common "
                     "paths (/blog, /resources, /news...). Posts rendered only by JavaScript can't be seen by this scraper.")
    if sc.blocked_hits:
        res["notes"].append(f"{sc.blocked_hits} request(s) were blocked by the site, so results may be incomplete.")
    db.touch_site(domain, sc.base)

    known = db.known_urls(domain)
    unseen = sorted((u for u in seeds if u not in known),
                    key=lambda u: seeds[u].get("published") or "", reverse=True)
    stamp, new_posts, budget = db.now(), [], MAX_FETCH
    for u in unseen:
        if sc.timed_out:
            res["notes"].append("Time limit reached. Remaining posts will be picked up on the next check.")
            break
        seed = seeds[u]
        if budget > 0:
            budget -= 1
            d = sc.fetch_post(u, seed)
            if d is None:  # not an article (or 404): remember so we don't refetch it
                db.insert_post({"url": u, "domain": domain, "kind": "rejected", "first_seen": stamp, "run_id": run_id})
                continue
        else:
            d = sc.shallow(u, seed)
        d.update(url=u, domain=domain, kind="post", first_seen=stamp, run_id=run_id)
        d["keyword"] = enrich.derive_keyword(d["title"], u, d["description"], d["headings"])
        db.insert_post(d)
        new_posts.append(d)

    for row in db.shallow_posts(domain, BACKFILL):  # hydrate older shallow records; never flagged as new
        if sc.timed_out:
            break
        d = sc.fetch_post(row["url"], {"source": row["source"], "published": row["published"], "title": row["title"]})
        if d and d["detail"]:
            db.update_post(row["url"], title=d["title"], published=d["published"], description=d["description"],
                           headings=d["headings"], detail=1,
                           keyword=enrich.derive_keyword(d["title"], row["url"], d["description"], d["headings"]))

    rejected = db.rejected_urls(domain)
    new_posts.sort(key=_sort_key, reverse=True)
    listed = new_posts[:BASELINE_ENRICH] if first_run else new_posts
    to_look_up = listed[:ENRICH_CAP]
    api = {"status": "skipped", "message": "", "provider": "none"}
    if to_look_up:
        data, status, msg, provider = enrich.lookup_volumes([p["keyword"] for p in to_look_up], kw_provider, kw_key)
        api = {"status": status, "message": msg, "provider": provider}
        for p in to_look_up:
            hit = data.get(p["keyword"].lower())
            p["enrich_status"] = status if status != "ok" else ("ok" if hit else "no_data")
            if hit:
                p.update(volume=hit.get("volume"), cpc=hit.get("cpc"), competition=hit.get("competition"))
            db.update_post(p["url"], enrich_status=p["enrich_status"], volume=p.get("volume"),
                           cpc=p.get("cpc"), competition=p.get("competition"))
    for p in listed[ENRICH_CAP:]:
        p["enrich_status"] = "skipped"

    pricing = None
    try:
        pricing = _pricing_diff(domain, sc.pricing())
    except Exception as e:  # pricing is a bonus; never fail the run on it
        res["notes"].append(f"Pricing check failed: {type(e).__name__}")
    price_alert = pricing if pricing and pricing["changed"] else None

    alert_status = {}
    if notify and (listed or price_alert):
        head, slack_text, plain, body = alerts.build(domain, listed, first_run, len(new_posts), price_alert, prev_checked)
        alert_status = alerts.send(head, slack_text, plain, body, slack_webhook, email_to)
        res["alert_preview"] = plain

    summary = (f"Baseline: {len(new_posts)} posts indexed." if first_run
               else f"{len(new_posts)} new post(s)." if new_posts else "No new posts.")
    db.finish_run(run_id, "partial" if res["notes"] else "ok", summary, len(seeds), len(new_posts), bool(price_alert))
    res.update(message=summary, found_total=len([u for u in seeds if u not in rejected]), new_count=len(new_posts), new_posts=listed,
               pricing=pricing, keyword_api=api, alerts=alert_status, previous_check=prev_checked)
    return res