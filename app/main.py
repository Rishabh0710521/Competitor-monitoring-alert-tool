import os
import threading
import time
from collections import defaultdict, deque

from flask import Flask, jsonify, request, send_from_directory

from . import db, monitor
from .scraper import normalize_domain

app = Flask(__name__, static_folder="static", static_url_path="/static")
db.init()
_locks, _hits = defaultdict(threading.Lock), defaultdict(deque)
RATE = int(os.getenv("RATE_LIMIT_PER_HOUR", "30"))


def _dom(raw):
    return normalize_domain(raw)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/check")
def check():
    body = request.get_json(silent=True) or {}
    ip, q = request.headers.get("X-Forwarded-For", request.remote_addr), None
    q = _hits[ip]
    while q and time.time() - q[0] > 3600:
        q.popleft()
    if len(q) >= RATE:
        return jsonify(ok=False, status="rate_limited", message="Too many checks this hour. Try again later."), 429
    try:
        domain = _dom(body.get("domain"))
    except ValueError as e:
        return jsonify(ok=False, status="invalid", message=str(e)), 400
    lock = _locks[domain]
    if not lock.acquire(blocking=False):
        return jsonify(ok=False, status="busy", message=f"A check for {domain} is already running."), 409
    q.append(time.time())
    try:
        res = monitor.run_check(domain, slack_webhook=(body.get("slack_webhook") or "").strip() or None,
                                email_to=(body.get("email") or "").strip() or None,
                                kw_provider=body.get("kw_provider"), kw_key=(body.get("kw_key") or "").strip() or None)
        return jsonify(res)
    except Exception as e:  # last-resort guard so the UI always gets JSON
        app.logger.exception("check failed")
        return jsonify(ok=False, status="error", message=f"Unexpected error: {type(e).__name__}"), 500
    finally:
        lock.release()


@app.get("/api/sites")
def sites():
    return jsonify(db.sites())


@app.get("/api/sites/<domain>")
def site(domain):
    try:
        d = _dom(domain)
    except ValueError as e:
        return jsonify(message=str(e)), 400
    return jsonify(site=db.get_site(d), posts=db.posts(d), runs=db.runs(d), pricing=db.latest_pricing(d))


@app.delete("/api/sites/<domain>")
def reset(domain):
    db.delete_site(_dom(domain))
    return jsonify(ok=True)


def _scheduler(hours):
    while True:
        time.sleep(hours * 3600)
        for s in db.sites():
            try:
                monitor.run_check(s["domain"])
            except Exception:
                app.logger.exception("scheduled check failed for %s", s["domain"])


if float(os.getenv("CHECK_INTERVAL_HOURS", "0")) > 0:
    threading.Thread(target=_scheduler, args=(float(os.environ["CHECK_INTERVAL_HOURS"]),), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
