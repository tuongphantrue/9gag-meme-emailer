#!/usr/bin/env python3
"""
Bao Tin Manh Hai Gold Price -> Email (runs on GitHub Actions, no local computer needed)

Same shape as the 9gag-meme-emailer this is modeled on: fetches the current
price table from baotinmanhhai.vn, then emails an HTML digest via Gmail
SMTP. Runs in two phases so the workflow can persist dedup state *between*
them (see the accompanying GitHub Actions workflow):

    python gold_price_emailer.py generate
        -> scrapes the price table, writes the composed email
           (subject/html/text) under ./email/, and updates the
           "last sent price" state file

    python gold_price_emailer.py send
        -> reads ./email/* and sends it via Gmail SMTP

Unlike the meme bot (which dedups by post ID so it never re-sends the same
meme), there's no natural "ID" for a price snapshot. Instead this dedups by
*content*: if SEND_ONLY_ON_CHANGE=true (default) and the scraped prices are
byte-for-byte identical to the last run's, `generate` skips writing an
email at all, so you don't get an inbox full of unchanged prices every 3
hours.

SETUP
-----
1. Install dependencies:
       pip install requests beautifulsoup4

2. Create a Gmail "App Password" (regular Gmail passwords won't work with SMTP):
       - Go to https://myaccount.google.com/apppasswords
       - You need 2-Step Verification turned on first.
       - Create an app password for "Mail" and copy the 16-character code.

3. Set these as environment variables (see README.md for GitHub Actions
   secrets instead, if running in the cloud):
       export GMAIL_ADDRESS="youraddress@gmail.com"
       export GMAIL_APP_PASSWORD="16-char-app-password"
       export GOLD_RECIPIENT="where-to-send@example.com"
       export SEND_ONLY_ON_CHANGE="true"          # optional, default true
       export TIMEZONE="Asia/Ho_Chi_Minh"          # optional, for the subject line
       export SOURCE_URL="https://baotinmanhhai.vn/gia-vang-hom-nay"  # optional
       export STATE_FILE="state/last_price.json"   # optional, dedup state file

SCHEDULING
----------
See README.md / GitHub Actions workflow in this repo for running this every
few hours in the cloud without needing your own computer on.

NOTE ON SCRAPING
-----------------
baotinmanhhai.vn's robots.txt disallows automated crawling of some paths.
This script hits the page a handful of times a day, which is low-impact,
but you're responsible for checking their current robots.txt / terms
before running it unattended long-term:
    https://baotinmanhhai.vn/robots.txt
The page markup can also change at any time — if `generate` reports 0
parsed rows, open the page, inspect the price table, and update
`_extract_rows` below.
"""

import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import certifi
import requests
import urllib3
from bs4 import BeautifulSoup

# Only silences the warning when ALLOW_INSECURE_SSL_FALLBACK is actually
# used (see fetch_page) - the fallback path itself already prints its own
# explicit warning to stderr, so this just avoids a duplicate/confusing
# urllib3 warning on top of it.
if os.environ.get("ALLOW_INSECURE_SSL_FALLBACK", "false").lower() == "true":
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SOURCE_URL = os.environ.get("SOURCE_URL", "https://baotinmanhhai.vn/gia-vang-hom-nay")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

EMAIL_DIR = "email"

# Dedup state: a JSON file holding a hash of the last-emailed price table,
# so re-running every few hours only sends an email when prices actually
# moved, instead of spamming the same numbers repeatedly. The workflow is
# responsible for fetching this file from the state branch before
# `generate` runs, and for committing the updated version back afterward —
# this script only reads/writes the local path.
STATE_FILE = os.environ.get("STATE_FILE", "state/last_price.json")
SEND_ONLY_ON_CHANGE = os.environ.get("SEND_ONLY_ON_CHANGE", "true").lower() == "true"


def load_last_hash(path=STATE_FILE):
    """Return the previous run's price-table hash, or None if there isn't
    one (missing/corrupt state is treated as "first run", not fatal).
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f).get("hash")
    except (json.JSONDecodeError, OSError) as e:
        print(f"  could not read {path} ({e}) — starting with empty dedup state", file=sys.stderr)
        return None


def save_last_hash(price_hash, path=STATE_FILE):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"hash": price_hash, "updated": datetime.utcnow().isoformat() + "Z"}, f)


def hash_rows(rows):
    canonical = json.dumps(rows, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


ALLOW_INSECURE_SSL_FALLBACK = os.environ.get("ALLOW_INSECURE_SSL_FALLBACK", "false").lower() == "true"


def fetch_page(url=SOURCE_URL):
    """
    GET the page, verifying TLS against certifi's CA bundle explicitly.

    requests normally already uses certifi, but pip can end up with a
    stale certifi wheel cached in a CI runner, which shows up as
    'unable to get local issuer certificate' even though the site's
    certificate is fine. Pointing verify= at certifi.where() explicitly
    (rather than requests' default resolution) sidesteps that, and the
    workflow also upgrades certifi on every run to keep it fresh.

    If that still fails, ALLOW_INSECURE_SSL_FALLBACK=true retries once
    with TLS verification disabled — an explicit opt-in last resort, since
    it means the connection could be tampered with undetected. Leave it
    "false" unless you've confirmed via README's troubleshooting section
    that the failure really is a broken certificate chain on the site's
    end, not a MITM.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, verify=certifi.where())
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.SSLError as e:
        print(f"  TLS verification failed with certifi's CA bundle: {e}", file=sys.stderr)
        if not ALLOW_INSECURE_SSL_FALLBACK:
            print(
                "  Set ALLOW_INSECURE_SSL_FALLBACK=true to retry without verification "
                "as a last resort (see README troubleshooting section first).",
                file=sys.stderr,
            )
            raise
        print("  ALLOW_INSECURE_SSL_FALLBACK=true - retrying with TLS verification disabled.", file=sys.stderr)
        resp = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        resp.raise_for_status()
        return resp.text


def parse_gold_prices(html):
    """
    Parse the gold price table: rows of <product name> | <buy> | <sell>,
    in VND per "chi" (1 chi = 1/10 luong = 3.75g) unless the row says
    otherwise.

    Tries a table-based strategy first, falls back to a regex scan of the
    page text if no <table> matches (the markup on these sites changes
    fairly often).
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = _extract_rows(soup)
    if not rows:
        rows = _extract_rows_fallback(soup.get_text(" ", strip=True))
    return rows


def _extract_rows(soup):
    results = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) >= 3 and _looks_like_price(cells[1]):
                results.append({"product": cells[0], "buy": cells[1], "sell": cells[2]})
    return results


def _extract_rows_fallback(text):
    pattern = re.compile(
        r"([A-Za-zÀ-ỹ0-9À-ỹ\.\(\)\s]{4,60}?)\s+([\d\.,]{6,15})\s*(?:đ|VNĐ)?\s*/?\s*"
        r"(?:chỉ|lượng)?\s+([\d\.,]{6,15})"
    )
    results = []
    for match in pattern.finditer(text):
        name, buy, sell = match.groups()
        name = name.strip()
        if len(name) >= 4:
            results.append({"product": name, "buy": buy, "sell": sell})
    return results[:30]  # sanity cap


def _looks_like_price(s):
    digits = re.sub(r"[^\d]", "", s)
    return digits.isdigit() and len(digits) >= 6


def build_html(rows, source_url, timestamp):
    if not rows:
        body = (
            "<p>Could not parse any price rows this run. The page structure "
            f"may have changed — check <a href='{escape(source_url)}'>{escape(source_url)}</a> "
            "directly, and update the parser's CSS selectors in gold_price_emailer.py.</p>"
        )
    else:
        row_html = "\n".join(
            f"<tr>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{escape(r['product'])}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['buy'])}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['sell'])}</td>"
            f"</tr>"
            for r in rows
        )
        body = f"""
        <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
          <thead>
            <tr style="background:#f5f5f5;">
              <th style="padding:8px 12px;text-align:left;">Sản phẩm</th>
              <th style="padding:8px 12px;text-align:right;">Mua vào</th>
              <th style="padding:8px 12px;text-align:right;">Bán ra</th>
            </tr>
          </thead>
          <tbody>
            {row_html}
          </tbody>
        </table>"""

    return f"""\
<html>
  <body style="margin:0; padding:20px; background:#f4f4f4; font-family:Arial,Helvetica,sans-serif;">
    <h1 style="color:#b8860b;">Giá vàng Bảo Tín Mạnh Hải</h1>
    <p style="color:#555;">Cập nhật {escape(timestamp)}</p>
    {body}
    <p style="color:#999; font-size:12px; margin-top:20px;">
      Nguồn: <a href="{escape(source_url)}">{escape(source_url)}</a> ·
      Đơn vị: đồng/chỉ trừ khi ghi chú khác · Email tự động, chỉ mang tính tham khảo, không phải lời khuyên đầu tư.
    </p>
  </body>
</html>"""


def build_plain_text(rows, source_url, timestamp):
    lines = [f"Gia vang Bao Tin Manh Hai - cap nhat {timestamp}", ""]
    if not rows:
        lines.append("Could not parse any price rows this run.")
    else:
        for r in rows:
            lines.append(f"{r['product']}: mua {r['buy']} / ban {r['sell']}")
    lines.append("")
    lines.append(f"Nguon: {source_url}")
    return "\n".join(lines)


def resolve_timestamp():
    timezone_name = os.environ.get("TIMEZONE", "Asia/Ho_Chi_Minh")
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        now = datetime.now()
    return now, now.strftime("%H:%M %d/%m/%Y")


def cmd_generate():
    if os.path.exists(EMAIL_DIR):
        for f in os.listdir(EMAIL_DIR):
            os.remove(os.path.join(EMAIL_DIR, f))
    os.makedirs(EMAIL_DIR, exist_ok=True)

    print(f"Fetching {SOURCE_URL} ...")
    try:
        html = fetch_page()
    except requests.RequestException as e:
        print(f"Failed to fetch page: {e}", file=sys.stderr)
        sys.exit(1)

    rows = parse_gold_prices(html)
    print(f"Parsed {len(rows)} price row(s).")

    price_hash = hash_rows(rows)
    last_hash = load_last_hash()

    if rows and SEND_ONLY_ON_CHANGE and price_hash == last_hash:
        print("Prices unchanged since last run and SEND_ONLY_ON_CHANGE=true - skipping email.")
        with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
            json.dump({"send": False}, f)
        return

    now, timestamp = resolve_timestamp()
    subject = f"Gia vang BTMH - {now.strftime('%d/%m/%Y %H:%M')}"
    html_body = build_html(rows, SOURCE_URL, timestamp)
    text_body = build_plain_text(rows, SOURCE_URL, timestamp)

    with open(os.path.join(EMAIL_DIR, "subject.txt"), "w") as f:
        f.write(subject)
    with open(os.path.join(EMAIL_DIR, "body.html"), "w") as f:
        f.write(html_body)
    with open(os.path.join(EMAIL_DIR, "body.txt"), "w") as f:
        f.write(text_body)
    with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
        json.dump({"send": True, "row_count": len(rows)}, f)

    # Only persist the new hash once the email has actually been composed,
    # mirroring the meme bot's "mark as sent only after it's queued" logic.
    save_last_hash(price_hash)
    print(f"Generated email with {len(rows)} row(s). Saved to ./{EMAIL_DIR}/")


def cmd_send():
    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("GOLD_RECIPIENT")

    missing = [name for name, val in [
        ("GMAIL_ADDRESS", sender),
        ("GMAIL_APP_PASSWORD", app_password),
        ("GOLD_RECIPIENT", recipient),
    ] if not val]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    meta_path = os.path.join(EMAIL_DIR, "meta.json")
    if not os.path.exists(meta_path):
        print("No meta.json found - run 'generate' first.", file=sys.stderr)
        sys.exit(1)

    with open(meta_path) as f:
        meta = json.load(f)
    if not meta.get("send", False):
        print("Nothing to send this run (unchanged prices, or generate found no rows).")
        return

    with open(os.path.join(EMAIL_DIR, "subject.txt")) as f:
        subject = f.read()
    with open(os.path.join(EMAIL_DIR, "body.html")) as f:
        html_body = f.read()
    with open(os.path.join(EMAIL_DIR, "body.txt")) as f:
        text_body = f.read()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, app_password)
        server.send_message(msg)

    print(f"Sent to {recipient}!")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("generate", "send"):
        print("Usage: python gold_price_emailer.py [generate|send]", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "generate":
        cmd_generate()
    else:
        cmd_send()


if __name__ == "__main__":
    main()
