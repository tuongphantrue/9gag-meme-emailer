#!/usr/bin/env python3
"""
9gag Top Meme of the Day -> Email

Fetches the current #1 post from 9gag's "hot" feed and emails it as an
image attachment via Gmail.

SETUP
-----
1. Install dependency:
       pip install requests

2. Create a Gmail "App Password" (regular Gmail passwords won't work with SMTP):
       - Go to https://myaccount.google.com/apppasswords
       - You need 2-Step Verification turned on first.
       - Create an app password for "Mail" and copy the 16-character code.

3. Set these as environment variables (recommended, keeps secrets out of the file):
       export GMAIL_ADDRESS="youraddress@gmail.com"
       export GMAIL_APP_PASSWORD="16-char-app-password"
       export MEME_RECIPIENT="where-to-send@example.com"

   (On Windows, use `setx` or set them in Task Scheduler's action instead.)

4. Run it:
       python 9gag_top_meme_emailer.py

SCHEDULING
----------
- macOS/Linux: add a cron entry, e.g. run every day at 9am:
      0 9 * * * /usr/bin/python3 /path/to/9gag_top_meme_emailer.py >> /path/to/log.txt 2>&1
  Edit with: crontab -e

- Windows: use Task Scheduler to run this script daily
  (Action = "Start a program", Program = python.exe, Arguments = path to this script).

- No always-on computer: use a free scheduler like GitHub Actions (a scheduled
  workflow) or cron-job.org hitting a small hosted version of this script.
  Ask me and I can set up whichever option you pick.
"""

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

import requests

# 9gag's unofficial hot-feed API (no auth required for SFW content)
NINEGAG_HOT_URL = "https://9gag.com/v1/group-posts/group/hot/type/hot"


def get_top_meme():
    """Return (title, image_bytes, filename) for today's #1 hot post."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MemeBot/1.0)"}
    resp = requests.get(NINEGAG_HOT_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    posts = data.get("data", {}).get("posts", [])
    if not posts:
        raise RuntimeError("No posts returned from 9gag API")

    # Posts are already ranked by "hot" score; posts[0] is the top one.
    # Skip any NSFW or video-only posts, take the first clean image post.
    top_post = None
    for post in posts:
        if post.get("nsfw", 0):
            continue
        if post.get("type") not in ("Photo", "Animated"):
            continue
        top_post = post
        break

    if top_post is None:
        raise RuntimeError("No suitable (SFW image) post found in hot feed")

    title = top_post.get("title", "Untitled meme")
    image_url = top_post["images"]["image700"]["url"]

    img_resp = requests.get(image_url, headers=headers, timeout=15)
    img_resp.raise_for_status()

    ext = image_url.split(".")[-1].split("?")[0]
    filename = f"top_meme_of_the_day.{ext}"

    return title, img_resp.content, filename


def send_email(subject, body, image_bytes, filename, sender, app_password, recipient):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    maintype = "image"
    subtype = filename.split(".")[-1]
    if subtype == "jpg":
        subtype = "jpeg"

    msg.add_attachment(image_bytes, maintype=maintype, subtype=subtype, filename=filename)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, app_password)
        server.send_message(msg)


def main():
    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("MEME_RECIPIENT")

    missing = [name for name, val in [
        ("GMAIL_ADDRESS", sender),
        ("GMAIL_APP_PASSWORD", app_password),
        ("MEME_RECIPIENT", recipient),
    ] if not val]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    print("Fetching top meme from 9gag...")
    title, image_bytes, filename = get_top_meme()
    print(f"Found: {title}")

    print(f"Emailing to {recipient}...")
    send_email(
        subject=f"Top meme of the day: {title}",
        body=f"Today's top meme from 9gag:\n\n{title}",
        image_bytes=image_bytes,
        filename=filename,
        sender=sender,
        app_password=app_password,
        recipient=recipient,
    )
    print("Sent!")


if __name__ == "__main__":
    main()
