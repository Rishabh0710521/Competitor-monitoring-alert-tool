"""Alerts: Slack-compatible webhook and/or SMTP email."""
import html
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from .scraper import SiteError, check_public

MAX_LISTED = 15


def _vol(p):
    if p.get("volume") is not None:
        return f"{p['volume']:,}/mo"
    return {"no_api_key": "n/a (no keyword API key)", "invalid_api_key": "n/a (invalid API key)",
            "quota_exceeded": "n/a (API quota used up)", "rate_limited": "n/a (API rate limited)",
            "skipped": "not looked up"}.get(p.get("enrich_status"), "n/a")


def build(domain, posts, first_run, total, pricing_change, prev_checked):
    """Returns (headline, slack_text, plain_text, html_body)."""
    n = len(posts)
    if first_run:
        head = f"Now tracking {domain}: {total} posts indexed, latest {n} below"
    elif n:
        since = f" since {prev_checked[:10]}" if prev_checked else ""
        head = f"{domain} published {n} new post{'s' if n != 1 else ''}{since}"
    else:
        head = f"{domain} changed its pricing"
    esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    slack, plain, rows = [f"*{esc(head)}*"], [head, ""], []
    for p in posts[:MAX_LISTED]:
        when = f" ({p['published']})" if p.get("published") else ""
        slack.append(f"• <{p['url']}|{esc(p['title'])}>{when}\n   keyword: `{p.get('keyword') or 'n/a'}` · volume: {_vol(p)}")
        plain += [f"- {p['title']}{when}", f"  {p['url']}", f"  keyword: {p.get('keyword') or 'n/a'} | volume: {_vol(p)}"]
        rows.append(f"<li><a href='{html.escape(p['url'])}'>{html.escape(p['title'])}</a>{html.escape(when)}<br>"
                    f"keyword: <b>{html.escape(p.get('keyword') or 'n/a')}</b> &middot; volume: {html.escape(_vol(p))}</li>")
    if n > MAX_LISTED:
        slack.append(f"…and {n - MAX_LISTED} more")
        plain.append(f"...and {n - MAX_LISTED} more")
    body = f"<h3>{html.escape(head)}</h3><ul>{''.join(rows)}</ul>"
    if pricing_change:
        slack.append(f"*Pricing changed* ({pricing_change['url']})")
        plain += ["", f"Pricing changed: {pricing_change['url']}"]
        body += f"<h4>Pricing changed</h4><p>{html.escape(pricing_change['url'])}</p><ul>"
        for tag, items in (("Added", pricing_change["added"]), ("Removed", pricing_change["removed"])):
            for line in items[:10]:
                slack.append(f"   {tag.lower()}: {esc(line)}")
                plain.append(f"  {tag}: {line}")
                body += f"<li>{tag}: {html.escape(line)}</li>"
        body += "</ul>"
    return head, "\n".join(slack), "\n".join(plain), body


def send(head, slack_text, plain, body, webhook=None, email_to=None):
    """Returns {"slack": status, "email": status}; failures are reported, never raised."""
    out = {}
    webhook = webhook or os.getenv("SLACK_WEBHOOK_URL")
    if not webhook:
        out["slack"] = "skipped: no webhook URL"
    elif not webhook.startswith("https://"):
        out["slack"] = "failed: webhook must be an https:// URL"
    else:
        try:
            check_public(webhook)
            r = requests.post(webhook, json={"text": slack_text, "content": plain[:1900]}, timeout=10)
            out["slack"] = "sent" if r.status_code < 300 else f"failed: HTTP {r.status_code} from webhook"
        except SiteError as e:
            out["slack"] = f"failed: {e}"
        except requests.RequestException as e:
            out["slack"] = f"failed: {type(e).__name__}"
    to = email_to or os.getenv("ALERT_EMAIL")
    host = os.getenv("SMTP_HOST")
    if not to:
        out["email"] = "skipped: no recipient"
    elif not host:
        out["email"] = "skipped: SMTP not configured on the server"
    else:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"], msg["To"] = head, to
            msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER", "alerts@localhost")
            msg.attach(MIMEText(plain, "plain"))
            msg.attach(MIMEText(body, "html"))
            with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=15) as s:
                if os.getenv("SMTP_STARTTLS", "1") == "1":
                    s.starttls()
                if os.getenv("SMTP_USER"):
                    s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS", ""))
                s.send_message(msg)
            out["email"] = "sent"
        except (smtplib.SMTPException, OSError) as e:
            out["email"] = f"failed: {type(e).__name__}"
    return out
