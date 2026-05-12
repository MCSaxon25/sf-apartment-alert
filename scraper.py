import feedparser
import json
import os
import smtplib
import re
from datetime import date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── Config ────────────────────────────────────────────────────────────────────
SEEN_FILE = "seen_listings.json"
RECIPIENT = "mcsaxon25@gmail.com"
SENDER = os.environ.get("GMAIL_ADDRESS")
APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
MOVE_IN_CUTOFF = date(2026, 8, 15)
PRICE_HARD_MAX = 5000

# Craigslist SF apartments RSS — pre-filtered to ≤$5k, min 1BR
CL_URLS = [
    "https://sfbay.craigslist.org/search/sfc/apa?format=rss&max_price=5000&min_bedrooms=1",
]

# ── Scoring tables ────────────────────────────────────────────────────────────
NEIGHBORHOOD_SCORES = {
    "inner richmond":        3,
    "pacific heights":       3,
    "pac heights":           3,
    "lower pacific heights": 3,
    "lower pac heights":     3,
    "nopa":                  2,
    "north of panhandle":    2,
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

def extract_price(text: str) -> int | None:
    for m in re.findall(r'\$[\d,]+', text):
        price = int(m.replace("$", "").replace(",", ""))
        if 500 < price < 15_000:
            return price
    return None

def extract_bedrooms(text: str) -> int | None:
    t = text.lower()
    if re.search(r'\b(studio|efficiency)\b', t):
        return 0
    m = re.search(r'(\d)\s*(?:br|bd|bed|bedroom)', t)
    if m:
        return int(m.group(1))
    for word, n in [("one", 1), ("two", 2), ("three", 3), ("four", 4)]:
        if re.search(rf'\b{word}\s+bed', t):
            return n
    return None

def detect_neighborhood(text: str):
    t = text.lower()
    # Sort by score desc so higher-priority names match first
    for name, score in sorted(NEIGHBORHOOD_SCORES.items(), key=lambda x: -x[1]):
        if name in t:
            return name, score
    return None, 0

def is_furnished(text: str) -> bool:
    t = text.lower()
    return bool(re.search(r'\bfurnished\b', t)) and not bool(re.search(r'\bunfurnished\b', t))

def extract_move_in(text: str) -> date | None:
    MONTHS = {
        'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
        'july':7,'august':8,'september':9,'october':10,'november':11,'december':12,
        'jan':1,'feb':2,'mar':3,'apr':4,'jun':6,'jul':7,'aug':8,
        'sep':9,'oct':10,'nov':11,'dec':12,
    }
    t = text.lower()

    # MM/DD or MM/DD/YY(YY)
    m = re.search(
        r'(?:available|avail|move.in|avail from)[:\s]+(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?', t
    )
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else 2026
        if year < 100: year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            pass

    # "available June 1" / "available August"
    for name, num in MONTHS.items():
        pat = (
            rf'(?:available|avail|move.in)[:\s]+'
            rf'(?:(?:the\s+)?(\d{{1,2}})\s+(?:of\s+)?)?{name}'
            rf'(?:[^\d]*(\d{{1,2}}))?(?:,?\s+(\d{{4}}))?'
        )
        m = re.search(pat, t)
        if m:
            day = int(m.group(1) or m.group(2) or 1)
            year = int(m.group(3)) if m.group(3) else 2026
            try:
                return date(year, num, day)
            except ValueError:
                pass
    return None

# ── Scoring ───────────────────────────────────────────────────────────────────
def score_listing(title: str, body: str, price: int | None):
    score = 0
    reasons = []
    text = f"{title} {body}".lower()

    # Neighborhood
    nbr_name, nbr_pts = detect_neighborhood(text)
    if nbr_name:
        score += nbr_pts
        reasons.append(f"{nbr_name.title()}: +{nbr_pts}")

    # Bedrooms
    beds = extract_bedrooms(text)
    if beds and beds >= 2:
        score += 3;   reasons.append(f"{beds}BR: +3")
    elif beds == 1:
        score += 0.8; reasons.append("1BR: +0.8")

    # Price
    if price:
        if price <= 4500:
            score += 2.5; reasons.append(f"${price:,} (≤$4.5k): +2.5")
        else:
            score += 1.5; reasons.append(f"${price:,} ($4.5k–$5k): +1.5")

    # Amenities
    if re.search(r'w/d in unit|washer.dryer in unit|in.unit laundry|in unit w/d', text):
        score += 2;   reasons.append("W/D in-unit: +2")
    elif re.search(r'\bw/d\b|washer.dryer|laundry in build|on.site laundry|shared laundry', text):
        score += 0.8; reasons.append("W/D in building: +0.8")

    if re.search(r'\bparking\b|garage|parking included|\b1 car\b|one car', text):
        score += 1.5; reasons.append("Parking: +1.5")

    if re.search(r'\bdishwasher\b', text):
        score += 1;   reasons.append("Dishwasher: +1")

    if re.search(r'\bpatio\b|\bbalcony\b|\boutdoor space\b|\byard\b|\bdeck\b', text):
        score += 1.2; reasons.append("Outdoor space: +1.2")

    return score, reasons, beds

# ── Email ─────────────────────────────────────────────────────────────────────
def format_email(listings: list) -> str:
    rows = ""
    for apt in sorted(listings, key=lambda x: -x["score"]):
        beds_label = f"{apt['beds']}BR" if apt['beds'] else "?"
        price_label = f"${apt['price']:,}" if apt['price'] else "?"
        why = " · ".join(apt["reasons"])
        rows += f"""
        <tr>
          <td style="padding:14px 10px; border-bottom:1px solid #eee;">
            <a href="{apt['url']}" style="font-weight:600; color:#1a56db; text-decoration:none;">
              {apt['title']}
            </a><br>
            <span style="font-size:12px; color:#6b7280;">{why}</span>
          </td>
          <td style="padding:14px 10px; border-bottom:1px solid #eee; text-align:center; white-space:nowrap;">
            {price_label}
          </td>
          <td style="padding:14px 10px; border-bottom:1px solid #eee; text-align:center;">
            {beds_label}
          </td>
          <td style="padding:14px 10px; border-bottom:1px solid #eee; text-align:center; font-weight:700; color:#1a56db;">
            {apt['score']}
          </td>
        </tr>"""

    return f"""
    <html><body style="font-family: sans-serif; max-width:700px; margin:auto; color:#111;">
      <h2 style="margin-bottom:4px;">🏠 {len(listings)} new SF apartment match{"es" if len(listings)!=1 else ""}</h2>
      <p style="color:#6b7280; margin-top:0;">Sorted by score · Hard filters: ≤$5k, unfurnished, available before Aug 15</p>
      <table style="width:100%; border-collapse:collapse; font-size:14px;">
        <thead>
          <tr style="background:#f3f4f6; text-align:left;">
            <th style="padding:10px;">Listing</th>
            <th style="padding:10px; text-align:center;">Price</th>
            <th style="padding:10px; text-align:center;">Beds</th>
            <th style="padding:10px; text-align:center;">Score</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="font-size:12px; color:#9ca3af; margin-top:20px;">
        Sent by your SF Apartment Alert bot · 
        <a href="https://github.com" style="color:#9ca3af;">View repo</a>
      </p>
    </body></html>"""

def send_email(listings: list):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏠 {len(listings)} new SF apartment match{'es' if len(listings)!=1 else ''}"
    msg["From"] = SENDER
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(format_email(listings), "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER, APP_PASSWORD)
        server.sendmail(SENDER, RECIPIENT, msg.as_string())
    print(f"✅ Emailed {len(listings)} listing(s) to {RECIPIENT}")

# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    seen = load_seen()
    new_listings = []

    for url in CL_URLS:
        print(f"Fetching {url}")
        feed = feedparser.parse(url)
        print(f"  {len(feed.entries)} entries")

        for entry in feed.entries:
            listing_id = entry.get("id", entry.link)
            if listing_id in seen:
                continue

            title = entry.get("title", "")
            body  = entry.get("summary", "")
            full  = f"{title} {body}"

            # ── Hard filters ──────────────────────────────────────────────────
            price = extract_price(full) or extract_price(title)
            if price and price > PRICE_HARD_MAX:
                seen.add(listing_id); continue

            if is_furnished(full):
                seen.add(listing_id); continue

            move_in = extract_move_in(full)
            if move_in and move_in >= MOVE_IN_CUTOFF:
                seen.add(listing_id); continue

            nbr_name, _ = detect_neighborhood(full)
            if not nbr_name:
                seen.add(listing_id); continue

            beds = extract_bedrooms(full)
            if beds == 0:  # studios excluded
                seen.add(listing_id); continue

            # ── Score ─────────────────────────────────────────────────────────
            score, reasons, beds = score_listing(title, body, price)

            if score < 5:
                seen.add(listing_id); continue

            new_listings.append({
                "title":   title,
                "url":     entry.link,
                "score":   score,
                "price":   price,
                "beds":    beds,
                "reasons": reasons,
            })
            seen.add(listing_id)

    save_seen(seen)

    if new_listings:
        send_email(new_listings)
    else:
        print("No new matching listings.")

if __name__ == "__main__":
    run()
