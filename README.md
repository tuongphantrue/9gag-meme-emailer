# Hanoi House/Land Price Emailer (runs on GitHub Actions, no local computer needed)

Emails you house/land prices for Hanoi by district (quận) and rural
district (huyện), pulled from 6 independent, verified sources,
automatically via GitHub's free scheduled-workflow runners.

## Important: read this before relying on it

Gold prices have a clean daily aggregator site with one simple table per
seller. **Hanoi housing prices don't have a real equivalent.** Most
Vietnamese real estate sites are raw listing boards (individual asking
prices, no computed average), some front their pages with Cloudflare-style
protection that blocklists GitHub Actions' shared runner IPs outright
(confirmed via testing: a flat `403 Forbidden` on every single request,
regardless of headers), and at least one genuinely good source
(guland.vn) explicitly disallows automated access in its `robots.txt` -
respected here on principle, not worked around.

Given that, this script treats every source as fully independent and
expendable - if a source errors, gets blocked, or its page structure
doesn't match what the parser expects, it's silently left out of that
run's email. No error placeholders, no partial-failure noise - just
whatever sources actually came through, each in its own clearly labeled
section, with a footer listing which ones made it in. If literally none
come through, no email gets sent at all rather than sending an empty one.

1. **Mogi.vn** ([gia-nha-dat](https://mogi.vn/gia-nha-dat)) - one blended
   average price/m² per district (house + land together), with a
   month-over-month % change. One page covers every Hanoi district.
2-5. **Batdongsan.com.vn**, one source per property type - confirmed
   working via the reader-proxy workaround (see below) - each a min/max
   price/m² range, fetched one page per district (12 main urban
   districts; outlying huyện don't appear to have these page types):
   - Nhà mặt phố (street-front houses)
   - Chung cư (apartments) - the one genuinely separate apartment table
   - Nhà riêng (regular houses)
   - Đất nền (land)
6. **Nhatot.com** - city-wide average price/m² by property type (Nhà
   riêng, Nhà phố, Nhà mặt tiền, Chung cư). One of the few sites that
   actually publishes a computed average rather than just listing prices.

**Sites checked and deliberately left out**, rather than added as
speculative entries that would just come back empty every run:
   alonhadat.com.vn, cafeland.vn, dothi.net, and homedy.com only publish
   individual listing prices - there's no computed district or city
   average anywhere on them for a parser to extract. guland.vn does
   publish real per-district averages, but its `robots.txt` disallows
   automated access, so it's excluded regardless of technical
   feasibility. If you know of a genuinely different aggregator (with
   real computed averages, not just listings), that's the kind of thing
   worth adding - `fetch_nhatot()` or `fetch_batdongsan_category()` in
   the script are good examples of how a source is wired in.

## Vietnamese text encoding

If Vietnamese diacritics ever show up mangled (e.g. "đất" rendering as
"đâ´t", extra stray ´ or \` marks appearing near vowels), that's a
character-encoding issue, not a data issue. `open()` without an explicit
encoding falls back to the OS locale's default, and some CI runner images
default to a non-UTF-8 locale - Python then writes/reads the file using
the wrong encoding, which can corrupt non-ASCII text on the round trip
even though the string was correct in memory right up until it hit disk.
Every file read/write in this script now explicitly specifies
`encoding="utf-8"`, `MIMEText` explicitly declares `utf-8` as its charset
instead of relying on auto-detection, and the workflow sets
`PYTHONUTF8=1` plus `LANG`/`LC_ALL=en_US.UTF-8` at the job level as a
belt-and-suspenders fix at the interpreter/OS level too.

## Design

The email uses a warm ochre header band (the color of Hanoi's
French-colonial building facades throughout the Old Quarter) over a cream
content panel, with a serif/sans type pairing (Georgia for headings and
prices, Helvetica/Arial for labels and table data - both near-universal
system fonts, since email clients can't reliably load custom web fonts).
Each section has a small numbered "— Nguồn N" label instead of a plain
heading, and there's a quick stat strip up top (sources available /
fetched / district count) for an at-a-glance read before scrolling
through the tables.

Price-direction colors follow the **Vietnamese/Asian market convention**
(tăng/increase = red, giảm/decrease = green) rather than the US
convention (green = up) - the opposite of what an English-language
template would default to, but the correct one for this content and
audience.

All styling is inline (required for reliable rendering across email
clients, especially Gmail and Outlook) with a `<meta name="viewport">`
tag and a fluid-width container so it scales down cleanly on mobile. The
design tokens (colors, fonts) are defined once near the top of
`hanoi_house_price_emailer.py` (`C_*` and `F_*` constants) and reused
across every render function, so a palette or font change only needs to
happen in one place.

## Sample listings section

The email also includes a "Nhà mẫu tham khảo" (sample listings) section
with real, individual property listings - title, total price, area, a
photo, and a link - pulled from the same Batdongsan.com.vn pages already
being fetched for the price-range tables above.

**Price filter**: only listings at or under `LISTING_MAX_PRICE_TY` tỷ
đồng (default `2`, i.e. 2 billion VND) are shown. Listings priced "Giá
thỏa thuận" (negotiable, no figure given) are excluded rather than
assumed to pass, since there's no number to check against. Worth knowing:
"Nhà mặt phố" (street-front) and "Đất nền" (land) in central Hanoi
districts are almost never under 2 tỷ - even a small plot runs well past
that at the per-m² rates those categories show - so most matches
realistically come from "Chung cư" (apartments) and maybe outer-district
"Nhà riêng". Set `LISTING_MAX_PRICE_TY=` (empty) to disable the filter
and show everything found.

Up to `MAX_SAMPLE_LISTINGS` (default 15) are chosen at random from
whatever passes the filter. To have more to choose from,
`LISTING_CANDIDATES_PER_DISTRICT` (default 6) controls how many
candidate listings are pulled from each district page before filtering -
raise this if a run isn't finding enough matches under the price cap.

Finding the *candidate* listings costs no extra requests (those pages
already contain listing cards alongside the aggregate price data), but
getting a *photo* for each one mostly doesn't either anymore - the real
photo URL comes directly out of the same card content (see the note
below on the card structure), so the separate per-listing `og:image`
fetch (`fetch_listing_image`) is now only a fallback for the rare listing
that doesn't have an embedded image, not the default path.

Only works when the reader-proxy path succeeds for the category/district
fetch (the listing cards are parsed from its Markdown output; a
direct-fetch fallback's raw HTML won't match the card-parsing regex), and
a district whose price-range page doesn't happen to show any matching
listing cards just contributes nothing here - no impact on the price
data either way. Purely illustrative, not a curated or complete listing
feed.

Note: an earlier version of the card-matching regex assumed "Ảnh đại
diện" appeared as plain link text (based on inspecting these pages
through a different fetch path than what the actual GitHub Actions
workflow uses) - it never does. A production diagnostic dump confirmed
the real structure: each photo is a proper Markdown image tag
(`![Image N: Ảnh đại diện](https://file4.batdongsan.com.vn/...)`) nested
*inside* the listing's outer link. Fixed by matching on the real
structure, distinguishing the listing's own closing link
(`](https://batdongsan.com.vn/...)`) from the embedded images' closings
(`](https://file4.batdongsan.com.vn/...)`) by host. This also means the
real photo URL now comes for free out of the same content already being
fetched for price data - the separate per-listing `og:image` fetch
(`fetch_listing_image`) is now only a fallback for the rare listing that
doesn't have an image, not the default path.

That fix still wasn't enough on its own, though - listings were *still*
coming back at 0, and the diagnostic was reporting "'Ảnh đại diện' does
not appear anywhere" even in responses whose own printed snippet clearly
showed that exact text. That contradiction was the real clue: it's a
Unicode normalization mismatch. r.jina.ai appears to emit Vietnamese text
in NFD (decomposed) form, which displays - and even prints to a log -
identically to the NFC (precomposed) form used everywhere in this
script's source, but fails every substring/regex match between the two,
since they're different bytes underneath. Price-range extraction was
never affected because that path already ran fetched lines through
`unicodedata.normalize("NFC", ...)` as part of flattening them for
matching; listing-card extraction operated on the raw fetched text
directly and had no such step. Fixed at the actual source: `fetch_page()`
now normalizes to NFC before returning, on both the reader-proxy and
direct-fetch paths, so every consumer gets consistent text regardless of
what form the source site actually sent.

## Typical total price section

Below the per-district tables, the email adds a "Giá nhà điển hình tại
Hà Nội" (typical house prices) section - this isn't a new scrape, it's
arithmetic on the per-m² data already fetched: for each category (nhà mặt
phố, chung cư, nhà riêng, đất nền, and Mogi's blended average), it
averages that category's price/m² across whichever districts came back
this run, then multiplies by a few common sizes (30/50/70/100 m²) to give
an illustrative total price (e.g. "a typical 50m² chung cư costs about
2.25 - 4.5 tỷ"). It only shows categories that actually had data that run,
and it's explicitly labeled as an illustration, not the real price of any
specific property - actual prices depend heavily on exact location,
frontage, legal status, and condition. To change the reference sizes,
edit `TYPICAL_SIZES_M2` near the top of `hanoi_house_price_emailer.py`.

**If a run comes back with 0 rows for a source**, `fetch_page()` prints
diagnostics to the Action's log: the HTTP status code, response size, and
whether the response looks like a JS/anti-bot challenge page (Cloudflare
and similar) rather than real content.

As a workaround for IP-range blocks, `fetch_page()` tries a public
"reader" proxy (`r.jina.ai`) first - it fetches the page on its own
infrastructure (a different IP/fingerprint than GitHub's runners) and
returns the text, falling back to a direct fetch if that doesn't work
either. This is set via `USE_READER_PROXY=true` (the default). It's a
best-effort workaround, not a guarantee - the underlying sites could block
the proxy's IPs too, or change behavior at any time.

**The reader proxy itself has a request-rate limit.** With 4 Batdongsan
categories × 12 districts plus Mogi and Nhatot, a run fires off close to
50 requests - enough to trip a `429 Too Many Requests` on the proxy well
before any real site is even involved (this happened in testing: Nhà
riêng, Đất nền, and Nhatot all came back empty purely because they were
fetched *after* the proxy's budget was already used up by earlier
categories, not because those sites were blocking anything). To handle
this, `fetch_page()` retries a `429` with backoff (`READER_PROXY_MAX_RETRIES`,
default 3; `READER_PROXY_BACKOFF_SECONDS`, default 4, multiplied by the
attempt number) before falling back to a direct fetch, and paces every
request with a small delay (`READER_PROXY_PACING_SECONDS`, default 1.5s)
to avoid tripping the limit in the first place. This does mean a full run
now takes noticeably longer (several minutes rather than seconds) -
that's expected and fine for a scheduled background job with no time
pressure.

A "successful" (`200`, non-challenge) response that's suspiciously short
is also not trusted at face value - real district/category pages run
tens of KB; a response of only a few KB (this happened with Mogi.vn:
`200, 6346 bytes` but 0 rows parsed - far short of the 70-90KB a real
page runs) is more likely a stub or truncated render than the actual
content, so that case now falls through to a direct fetch too rather than
being silently accepted as "fetched fine, just empty". The threshold is
`READER_PROXY_MIN_BYTES` (default 10000). If a source still comes back
empty after all that, `fetch_mogi()` in particular now logs a text
snippet of whatever it did receive, so a repeat failure gives something
concrete to debug rather than another guess.

This already caught one real issue: Mogi.vn's page structure shifted
slightly (the price now sits a variable number of lines after the
district name instead of always exactly one line after), which broke the
original fixed-offset parser. It's now a bounded scan - look ahead until
either a price is found or the next known district name is hit - instead
of assuming a fixed offset, so it should tolerate small future markup
drift without breaking again.

**Nhatot.com looks like a harder block than the others.** Its
reader-proxy response came back as a tiny ~500-byte stub (likely the
proxy's own "this page couldn't be fetched" response) and the direct
fetch hit a real Cloudflare "just a moment" JS challenge - not a rate
limit, an actual challenge requiring JavaScript execution to pass.
Neither the reader proxy nor a plain HTTP client can solve that; getting
past it would need a genuine headless-browser fetch, which is a
meaningfully bigger piece of infrastructure than this script currently
uses. It's left in the source list since it's harmless (skipped silently
like anything else), but don't expect it to start working without that
kind of change.

If a source still comes back empty even after retries and pacing, the
realistic remaining options are: running this from a non-cloud/residential
IP instead of GitHub Actions (e.g. your own computer via cron), or a paid
scraping API service that maintains residential IPs - both add real
cost/complexity for what's meant to be a simple free digest, worth
weighing against just checking prices manually.

Also worth knowing: unlike gold, this data does not update every 30
minutes - Mogi appears to refresh it roughly monthly. Running the workflow
every 30 minutes will very often just re-send the same numbers unless you
turn on `SEND_ONLY_ON_CHANGE` (see below).

## One-time setup (~5 minutes)

1. **Create a GitHub account** if you don't have one: <https://github.com/join>

2. **Create a new repository**
   - Click "+" (top right) -> "New repository"
   - Name it anything, e.g. `hanoi-house-price-emailer`
   - Set it to **Private** (recommended, keeps your workflow config private)
   - Click "Create repository"

3. **Upload these files** to the repo (drag-and-drop works fine via the
   GitHub web UI: "Add file" -> "Upload files"), keeping the folder structure:
   - `hanoi_house_price_emailer.py`
   - `requirements.txt`
   - `.github/workflows/send-house-price.yml`

4. **Create a Gmail App Password** (your normal Gmail password won't work):
   - Turn on 2-Step Verification: <https://myaccount.google.com/signinoptions/two-step-verification>
   - Then create an app password: <https://myaccount.google.com/apppasswords>
   - Choose "Mail" as the app, copy the 16-character password it gives you.

5. **Add your secrets to the repo** (this keeps your email/password out of the code):
   - In your repo: Settings -> Secrets and variables -> Actions -> "New repository secret"
   - Add three secrets:
     * `GMAIL_ADDRESS` = your Gmail address
     * `GMAIL_APP_PASSWORD` = the 16-character app password from step 4
     * `HOUSE_RECIPIENT` = the email address that should receive the price update

6. **Test it manually**
   - Go to the "Actions" tab in your repo
   - Click "Send Hanoi House Price" on the left
   - Click "Run workflow" -> "Run workflow" (green button)
   - Wait ~10-15 seconds, refresh, click into the run to see logs / confirm success
   - Check the recipient inbox for the email

That's it - from now on it runs automatically on the schedule below.

## Changing the schedule

Open `.github/workflows/send-house-price.yml` and edit this line:

```
- cron: "*/30 * * * *"
```

Cron format is `minute hour day month weekday`, always in **UTC**. Given
how slowly this data actually moves, a daily or weekly cadence is probably
more sensible than every 30 minutes:

- `0 1 * * *` -> once a day at 1am UTC (8am Vietnam, UTC+7)
- `0 1 * * 1` -> once a week, Monday 1am UTC
- `*/30 * * * *` -> every 30 minutes (current setting)

## Only emailing on price changes

Currently `SEND_ONLY_ON_CHANGE` is `"false"` in the workflow, so **every**
scheduled run sends an email with that moment's prices, whether or not
they've moved since last time. Given this data barely changes between
runs, you'll likely want:

```
SEND_ONLY_ON_CHANGE: "true"
```

in the "Generate email" step of the workflow. With that on, `generate`
compares the freshly scraped prices against a hash saved from the last
run - stored in `state/last_price.json` on a dedicated `house-price-state`
branch the workflow creates/updates automatically - and skips the email if
nothing changed.

## Notes

- GitHub Actions free tier includes 2,000 minutes/month for private repos.
- You can also trigger it manually anytime via the "Run workflow" button.
- If the run fails, check the Actions tab -> the failed run -> logs. Common
  causes: a secret is missing/misspelled, the Gmail app password was
  revoked, or a source's page markup/anti-bot behavior changed (see below).
- If a run reports 0 rows for a source, check that source's diagnostic
  lines in the log (HTTP status, response size, and a note if the response
  looks like a JS/anti-bot challenge page). Open the source URL yourself
  in a browser to compare against what the log shows, and adjust
  `parse_mogi` / `parse_batdongsan_district` in
  `hanoi_house_price_emailer.py` if the page structure changed.
- Always worth checking the current `robots.txt` / terms before running
  this unattended long-term:
  <https://mogi.vn/robots.txt> and <https://batdongsan.com.vn/robots.txt>

## Running locally instead

```
pip install -r requirements.txt
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
export HOUSE_RECIPIENT="you@gmail.com"
python hanoi_house_price_emailer.py generate
python hanoi_house_price_emailer.py send
```

Schedule it yourself with cron (`crontab -e`):

```
0 1 * * * cd /path/to/hanoi-house-price-emailer && /usr/bin/python3 hanoi_house_price_emailer.py generate && /usr/bin/python3 hanoi_house_price_emailer.py send >> house_emailer.log 2>&1
```
