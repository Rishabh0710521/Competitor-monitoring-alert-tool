"""SQLite storage. `posts.url` is the canonical URL, so a re-run can never report the same post twice."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites(domain TEXT PRIMARY KEY, base TEXT, created_at TEXT, last_checked TEXT);
CREATE TABLE IF NOT EXISTS posts(
  url TEXT PRIMARY KEY, domain TEXT, kind TEXT DEFAULT 'post', title TEXT, published TEXT, description TEXT,
  headings TEXT, keyword TEXT, volume INTEGER, cpc REAL, competition REAL, enrich_status TEXT,
  detail INTEGER DEFAULT 1, source TEXT, first_seen TEXT, run_id INTEGER);
CREATE INDEX IF NOT EXISTS posts_domain ON posts(domain, kind);
CREATE TABLE IF NOT EXISTS pricing(id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, url TEXT,
  fingerprint TEXT, lines TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, started_at TEXT, finished_at TEXT,
  status TEXT, first_run INTEGER, found INTEGER, new_count INTEGER, pricing_changed INTEGER, message TEXT);
"""
POST_COLS = ["url", "domain", "kind", "title", "published", "description", "headings", "keyword", "volume", "cpc",
             "competition", "enrich_status", "detail", "source", "first_seen", "run_id"]


def now():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


@contextmanager
def conn():
    path = os.getenv("DB_PATH", "data/monitor.db")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def rows(sql, *args):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def get_site(domain):
    r = rows("SELECT * FROM sites WHERE domain=?", domain)
    return r[0] if r else None


def touch_site(domain, base):
    with conn() as c:
        c.execute("INSERT INTO sites(domain,base,created_at,last_checked) VALUES(?,?,?,?) "
                  "ON CONFLICT(domain) DO UPDATE SET base=excluded.base, last_checked=excluded.last_checked",
                  (domain, base, now(), now()))


def known_urls(domain):
    return {r["url"] for r in rows("SELECT url FROM posts WHERE domain=?", domain)}


def rejected_urls(domain):
    return {r["url"] for r in rows("SELECT url FROM posts WHERE domain=? AND kind='rejected'", domain)}


def insert_post(p):
    cols = [k for k in POST_COLS if k in p]
    with conn() as c:
        c.execute(f"INSERT OR IGNORE INTO posts({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                  [p[k] for k in cols])


def update_post(url, **fields):
    with conn() as c:
        c.execute(f"UPDATE posts SET {','.join(k + '=?' for k in fields)} WHERE url=?", [*fields.values(), url])


def posts(domain, limit=200):
    return rows("SELECT * FROM posts WHERE domain=? AND kind='post' "
                "ORDER BY COALESCE(published,'') DESC, first_seen DESC LIMIT ?", domain, limit)


def shallow_posts(domain, limit):
    return rows("SELECT * FROM posts WHERE domain=? AND kind='post' AND detail=0 LIMIT ?", domain, limit)


def sites():
    return rows("SELECT s.*, (SELECT COUNT(*) FROM posts p WHERE p.domain=s.domain AND p.kind='post') AS post_count "
                "FROM sites s ORDER BY last_checked DESC")


def latest_pricing(domain):
    r = rows("SELECT * FROM pricing WHERE domain=? ORDER BY id DESC LIMIT 1", domain)
    if r:
        r[0]["lines"] = json.loads(r[0]["lines"])
    return r[0] if r else None


def add_pricing(domain, url, fingerprint, lines):
    with conn() as c:
        c.execute("INSERT INTO pricing(domain,url,fingerprint,lines,checked_at) VALUES(?,?,?,?,?)",
                  (domain, url, fingerprint, json.dumps(lines), now()))


def new_run(domain, first_run):
    with conn() as c:
        return c.execute("INSERT INTO runs(domain,started_at,status,first_run) VALUES(?,?,?,?)",
                         (domain, now(), "running", int(first_run))).lastrowid


def finish_run(run_id, status, message="", found=0, new_count=0, pricing_changed=False):
    with conn() as c:
        c.execute("UPDATE runs SET finished_at=?,status=?,message=?,found=?,new_count=?,pricing_changed=? WHERE id=?",
                  (now(), status, message, found, new_count, int(pricing_changed), run_id))


def runs(domain, limit=20):
    return rows("SELECT * FROM runs WHERE domain=? ORDER BY id DESC LIMIT ?", domain, limit)


def delete_site(domain):
    with conn() as c:
        for t in ("posts", "pricing", "runs", "sites"):
            c.execute(f"DELETE FROM {t} WHERE domain=?", (domain,))