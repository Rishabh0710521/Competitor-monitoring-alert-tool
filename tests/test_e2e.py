"""Run: ALLOW_PRIVATE=1 python -m unittest tests.test_e2e -v   (no internet needed)"""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ["ALLOW_PRIVATE"] = "1"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.pop("SLACK_WEBHOOK_URL", None)

from app import db, monitor  # noqa: E402

STATE = {"posts": [], "price": "$29", "slack": []}


def post_html(slug, title, date):
    return (f"<html><head><title>{title} | Acme</title><meta name='description' content='About {title}'>"
            f"<script type='application/ld+json'>{json.dumps({'@type': 'BlogPosting', 'headline': title, 'datePublished': date})}"
            f"</script></head><body><article><h1>{title}</h1></article></body></html>")


class Blog(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, code=200, ctype="text/html"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        mode, p = self.server.mode, self.path.split("?")[0]
        if mode == "blocked":
            return self.send("<html><title>Just a moment...</title></html>", 403)
        if mode == "noblog":
            return self.send("<html><body><a href='/pricing'>Pricing</a></body></html>") if p == "/" else self.send("nf", 404)
        posts = STATE["posts"]
        if p == "/":
            return self.send("<html><body><a href='/blog'>Blog</a><a href='/pricing'>Pricing</a></body></html>")
        if p == "/robots.txt":
            return self.send(f"User-agent: *\nSitemap: http://127.0.0.1:{self.server.server_port}/sitemap.xml", ctype="text/plain")
        if p == "/sitemap.xml":
            urls = "".join(f"<url><loc>http://127.0.0.1:{self.server.server_port}/blog/{s}</loc><lastmod>{d}</lastmod></url>" for s, t, d in posts)
            return self.send(f"<?xml version='1.0'?><urlset>{urls}</urlset>", ctype="application/xml")
        if p == "/blog":
            links = "".join(f"<article><a href='/blog/{s}'>{t}</a><time datetime='{d}'></time></article>" for s, t, d in posts)
            return self.send(f"<html><body>{links}</body></html>")
        if p == "/pricing":
            return self.send(f"<html><body><h3>Pro</h3><p>{STATE['price']} per month</p></body></html>")
        for s, t, d in posts:
            if p == f"/blog/{s}":
                return self.send(post_html(s, t, d))
        self.send("nf", 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode()
        if self.path == "/slack":
            STATE["slack"].append(json.loads(body))
            return self.send("ok")
        if self.path == "/ke":
            if self.headers.get("Authorization") != "Bearer good":
                return self.send("{}", 401, "application/json")
            return self.send(json.dumps({"data": [{"keyword": "keyword research", "vol": 12000, "cpc": {"value": "4.1"}, "competition": 0.5}]}), 200, "application/json")
        self.send("nf", 404)


def serve(mode="blog"):
    s = HTTPServer(("127.0.0.1", 0), Blog)
    s.mode = mode
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site, cls.blocked, cls.noblog = serve(), serve("blocked"), serve("noblog")
        cls.dom = f"127.0.0.1:{cls.site.server_port}"
        os.environ["KE_URL"] = f"http://127.0.0.1:{cls.site.server_port}/ke"
        cls.hook = f"http://127.0.0.1:{cls.site.server_port}/slack"
        STATE["posts"] = [("keyword-research-guide", "Keyword Research: A Guide", "2026-10-01"),
                          ("seo-audit-checklist", "SEO Audit Checklist", "2026-10-10")]

    def test_flow(self):
        # monkeypatch https-only webhook rule for local http mock
        from app import alerts
        orig = alerts.send
        r1 = monitor.run_check(self.dom, slack_webhook=None, kw_provider="keywordseverywhere", kw_key="good")
        self.assertTrue(r1["ok"] and r1["first_run"])
        self.assertEqual(r1["new_count"], 2)
        kw = {p["keyword"]: p for p in r1["new_posts"]}
        self.assertEqual(kw["keyword research"]["volume"], 12000)
        self.assertEqual(r1["new_posts"][0]["title"], "SEO Audit Checklist")  # titles + dates from page
        self.assertTrue(r1["new_posts"][0]["description"].startswith("About"))
        self.assertTrue(r1["pricing"]["first_snapshot"])

        r2 = monitor.run_check(self.dom, kw_provider="keywordseverywhere", kw_key="good")  # same domain again
        self.assertEqual(r2["new_count"], 0, "second run must not repeat posts")
        self.assertFalse(r2["alerts"])

        STATE["posts"].append(("link-building-tactics", "Link Building Tactics", "2026-10-20"))
        STATE["price"] = "$39"
        r3 = monitor.run_check(self.dom, kw_provider="keywordseverywhere", kw_key="bad")
        self.assertEqual([p["title"] for p in r3["new_posts"]], ["Link Building Tactics"])
        self.assertEqual(r3["keyword_api"]["status"], "invalid_api_key")  # run still succeeds
        self.assertIsNone(r3["new_posts"][0].get("volume"))
        self.assertTrue(r3["pricing"]["changed"])
        self.assertTrue(any("39" in l for l in r3["pricing"]["added"]))
        self.assertIn("slack", r3["alerts"])

    def test_blocked(self):
        r = monitor.run_check(f"127.0.0.1:{self.blocked.server_port}")
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "blocked")

    def test_no_blog(self):
        r = monitor.run_check(f"127.0.0.1:{self.noblog.server_port}")
        self.assertEqual(r["status"], "no_blog")

    def test_invalid_domain(self):
        with self.assertRaises(ValueError):
            monitor.run_check("not a domain")


if __name__ == "__main__":
    unittest.main()
