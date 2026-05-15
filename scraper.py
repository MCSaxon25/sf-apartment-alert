import json
import os
import re
import smtplib
import time
from datetime import date, datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

# ── Config ────────────────────────────────────────────────────────────────────
SEEN_FILE        = "seen_listings.json"
PENDING_FILE     = "pending_listings.json"
RECIPIENT        = "mcsaxon25@gmail.com"
SENDER           = os.environ.get("GMAIL_ADDRESS")
APP_PASSWORD     = os.environ.get("GMAIL_APP_PASSWORD")
MOVE_IN_CUTOFF   = date(2026, 8, 15)
PRICE_HARD_MAX   = 5000
MIN_SCORE        = 5

def is_daytime() -> bool:
    """Return True if current PT time is between 7am and 10pm.
    PDT = UTC-7, so 7am PT = 14:00 UTC, 10pm PT = 05:00 UTC next day.
    Daytime in UTC: hour >= 14 OR hour < 5
    """
    h = datetime.now(timezone.utc).hour
    return h >= 14 or h < 5

CL_URL = (
    "https://sfbay.craigslist.org/search/sfc/apa"
    "?max_price=5000&min_bedrooms=1&availabilityMode=0&sale_date=all+dates"
)

# ── Scoring ───────────────────────────────────────────────────────────────────
NEIGHBORHOOD_SCORES = {
    "inner richmond":        3,
    "pacific heights":       3,
    "pac heights":           3,
    "lower pacific heights": 3,
    "lower pac heights":     3,
    "presidio heights":      3,
    "marina":                3,
    "nopa":                  2,
    "north of panhandle":    2,
    "hayes valley":          2,
    "haight ashbury":        1,
    "haight-ashbury":        1,
    "upper haight":          1,
    "the haight":            1,
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)

def extract_price(text: str):
    for m in re.findall(r'\$[\d,]+', text):
        p = int(m.replace("$", "").replace(",", ""))
        if 500 < p < 15_000:
            return p
    return None

def extract_bedrooms(text: str):
    t = text.lower()
    if re.search(r'\b(studio|efficiency)\b', t):
        return 0
    m = re.search(r'(\d)\s*(?:br|bed|bedroom)', t)
    if m:
        return int(m.group(1))
    return None

def is_move_in_too_late(text: str) -> bool:
    t = text.lower()
    months = {
        "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
        "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
        "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,
        "sep":9,"oct":10,"nov":11,"dec":12,
    }
    for month_name, month_num in months.items():
        pattern = rf'{month_name}\s+(\d{{1,2}})'
        m = re.search(pattern, t)
        if m:
            day = int(m.group(1))
            try:
                move_in = date(2026, month_num, day)
                if move_in >= MOVE_IN_CUTOFF:
                    return True
            except ValueError:
                pass
    return False

def score_listing(title: str, body: str, price):
    text = (title + " " + body).lower()
    score = 0
    reasons = []

    # Neighborhood
    hood_score = 0
    for hood, pts in NEIGHBORHOOD_SCORES.items():
        if hood in text:
            hood_score = max(hood_score, pts)
    if hood_score == 0:
        return 0, ["not in target neighborhood"], None
    score += hood_score
    reasons.append(f"neighborhood +{hood_score}")

    # Bedrooms
    beds = extract_bedrooms(title + " " + body)
    if beds == 0:
        return 0, ["studio excluded"], beds
    elif beds == 2:
        score += 3; reasons.append("2BR +3")
    elif beds and beds >= 3:
        score += 2.5; reasons.append("3BR+ +2.5")
    elif beds == 1:
        score += 2.5; reasons.append("1BR +2.5")

    # Price
    if price is None:
        score += 1; reasons.append("price unknown +1")
    elif price <= 4500:
        score += 2.5; reasons.append("≤$4500 +2.5")
    elif price <= 5000:
        score += 1.5; reasons.append("≤$5000 +1.5")

    # Amenities
    if re.search(r'\bw[/\-]?d\b|washer.{0,10}dryer|in.unit laundry', text):
        if re.search(r'in.unit|in unit', text):
            score += 2; reasons.append("W/D in unit +2")
        else:
            score += 0.8; reasons.append("W/D in building +0.8")
    if re.search(r'\bparking\b|\bgarage\b|\bcarport\b', text):
        score += 1.5; reasons.append("parking +1.5")
    if re.search(r'\bpatio\b|\bdeck\b|\byard\b|\boutdoor\b|\bbalcony\b', text):
        score += 1.2; reasons.append("outdoor space +1.2")
    if re.search(r'\bdishwasher\b', text):
        score += 1; reasons.append("dishwasher +1")
    if re.search(r'\bpets ok\b|\bpet friendly\b|\bdogs ok\b|\bcats ok\b|\bpets welcome\b|\bpets allowed\b', text):
        score += 1.5; reasons.append("pets ok +1.5")

    return round(score, 1), reasons, beds

# ── Scraping with Playwright ──────────────────────────────────────────────────
def fetch_listings():
    listings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        print(f"Fetching {CL_URL}")
        page.goto(CL_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        # Debug: print a snippet of the HTML so we can see the real structure
        html = page.content()
        print(f"  Page length: {len(html)} chars")
        # Find first <li or first result-like element
        import re as _re
        snippet_match = _re.search(r'<li[\s][^>]{0,200}>', html)
        print(f"  First <li>: {snippet_match.group(0) if snippet_match else 'none found'}")
        # try to find any class with 'result' in it
        result_classes = _re.findall(r'class="([^"]*result[^"]*)"', html)
        print(f"  Result classes: {result_classes[:5]}")
        # try gallery/listing patterns
        for pattern in ["gallery-card", "cl-search-result", "result-row", "listing"]:
            idx = html.find(pattern)
            if idx > 0:
                print(f"  Found '{pattern}' at {idx}: ...{html[idx:idx+200]}...")
                break

        # Try multiple selectors
        selectors = [
            "li.cl-search-result",
            "li.gallery-card",
            ".cl-search-result",
            "li[data-pid]",
            ".result-row",
        ]
        items = []
        for sel in selectors:
            items = page.query_selector_all(sel)
            print(f"  Selector '{sel}': {len(items)} items")
            if items:
                break

        print(f"  Using {len(items)} listing elements")

        for item in items:
            try:
                link_el = item.query_selector("a.cl-app-anchor")
                title_el = item.query_selector(".label")
                price_el = item.query_selector(".priceinfo")
                hood_el  = item.query_selector(".meta")

                if not link_el or not title_el:
                    continue

                url   = link_el.get_attribute("href") or ""
                title = title_el.inner_text().strip()
                price_text = price_el.inner_text().strip() if price_el else ""
                hood_text  = hood_el.inner_text().strip()  if hood_el  else ""

                listing_id = url.split("/")[-1].replace(".html", "")

                listings.append({
                    "id":    listing_id,
                    "url":   url,
                    "title": title,
                    "price": price_text,
                    "meta":  hood_text,
                    "body":  "",
                })
            except Exception as e:
                print(f"  Error parsing item: {e}")

        browser.close()
    return listings

# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(matches: list, subject_prefix: str = "🏠"):
    rows = ""
    for m in matches:
        beds = m.get("beds")
        beds_label = f"{beds}BR" if beds is not None else "?"
        rows += f"""
        <tr>
          <td style="padding:12px;border-bottom:1px solid #eee;">
            <a href="{m['url']}" style="font-weight:bold;color:#1a0dab;text-decoration:none;">{m['title']}</a><br>
            <span style="color:#888;font-size:13px">{m['meta']}</span>
          </td>
          <td style="padding:12px;border-bottom:1px solid #eee;white-space:nowrap">{m['price']}</td>
          <td style="padding:12px;border-bottom:1px solid #eee;white-space:nowrap">{beds_label}</td>
          <td style="padding:12px;border-bottom:1px solid #eee;">{m['score']}</td>
          <td style="padding:12px;border-bottom:1px solid #eee;font-size:12px;color:#555">{', '.join(m['reasons'])}</td>
        </tr>"""

    html = f"""
    <html><body style="font-family:sans-serif;max-width:900px;margin:0 auto">
      <h2 style="color:#333">{subject_prefix} {len(matches)} SF Apartment{'s' if len(matches)>1 else ''}</h2>
      <table width="100%" cellspacing="0" style="border-collapse:collapse;border:1px solid #eee">
        <thead>
          <tr style="background:#f5f5f5">
            <th style="padding:10px;text-align:left">Listing</th>
            <th style="padding:10px;text-align:left">Price</th>
            <th style="padding:10px;text-align:left">Beds</th>
            <th style="padding:10px;text-align:left">Score</th>
            <th style="padding:10px;text-align:left">Why</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{subject_prefix} {len(matches)} new SF apartment{'s' if len(matches)>1 else ''} found"
    msg["From"]    = SENDER
    msg["To"]      = RECIPIENT
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(SENDER, APP_PASSWORD)
        s.sendmail(SENDER, RECIPIENT, msg.as_string())
    print(f"Email sent with {len(matches)} listings.")

# ── Pending (overnight) helpers ───────────────────────────────────────────────
def load_pending() -> list:
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            return json.load(f)
    return []

def save_pending(listings: list):
    with open(PENDING_FILE, "w") as f:
        json.dump(listings, f, indent=2)

def clear_pending():
    if os.path.exists(PENDING_FILE):
        os.remove(PENDING_FILE)

# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    now_utc = datetime.now(timezone.utc)
    now_pt = now_utc - timedelta(hours=7)
    print(f"UTC: {now_utc.strftime('%H:%M')} | PT: {now_pt.strftime('%I:%M %p')} | daytime={is_daytime()}")

    seen = load_seen()
    listings = fetch_listings()
    new_matches = []

    for item in listings:
        lid = item["id"]
        if lid in seen:
            continue
        seen.add(lid)

        title = item["title"]
        body  = item["meta"]
        price = extract_price(item["price"] + " " + title)

        # Hard filters
        if price and price > PRICE_HARD_MAX:
            print(f"  SKIP (price) {title[:60]}")
            continue
        if re.search(r'\bfurnished\b', (title + body).lower()) and \
           not re.search(r'\bunfurnished\b', (title + body).lower()):
            print(f"  SKIP (furnished) {title[:60]}")
            continue
        if is_move_in_too_late(title + " " + body):
            print(f"  SKIP (move-in too late) {title[:60]}")
            continue

        score, reasons, beds = score_listing(title, body, price)
        flag = "✅ MATCH" if score >= MIN_SCORE else "❌ skip "
        print(f"  {flag}  score={score}  {title[:60]}")

        if score >= MIN_SCORE:
            new_matches.append({**item, "score": score, "reasons": reasons, "beds": beds})

    save_seen(seen)

    if is_daytime():
        # ── Daytime: send immediately ──────────────────────────────────────────
        # First check if there's a leftover pending batch from overnight
        pending = load_pending()
        if pending:
            print(f"Sending overnight summary ({len(pending)} listings)...")
            send_email(pending, subject_prefix="🌙 Overnight summary")
            clear_pending()

        if new_matches:
            new_matches.sort(key=lambda x: x["score"], reverse=True)
            send_email(new_matches)
        else:
            print("No new matching listings.")
    else:
        # ── Overnight: accumulate into pending file ────────────────────────────
        if new_matches:
            pending = load_pending()
            pending.extend(new_matches)
            save_pending(pending)
            print(f"Overnight: saved {len(new_matches)} listings to pending (total pending: {len(pending)})")
        else:
            print("Overnight: no new matches, nothing added to pending.")

if __name__ == "__main__":
    run()
