#!/usr/bin/env python3
"""
truth_dashboard.py — Generate an HTML statistics dashboard from Trump's Truth Social RSS feed.

Usage:
    python truth_dashboard.py [--output dashboard.html] [--store posts.json] [--pages N]
    python truth_dashboard.py --no-fetch        # rebuild dashboard from stored posts only
    python truth_dashboard.py --enrich          # scrape engagement counts from Truth Social

"""

import argparse
import re
import sys
import json
import html
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:
    print("Error: 'requests' is required. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

DEFAULT_FEED_URL = "https://www.trumpstruth.org/feed"
DEFAULT_STORE = "posts.json"

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "not", "no", "nor",
    "so", "yet", "both", "either", "whether", "as", "if", "then", "than",
    "that", "this", "these", "those", "it", "its", "i", "we", "you", "he",
    "she", "they", "them", "their", "our", "your", "his", "her", "my",
    "who", "which", "what", "how", "when", "where", "why", "all", "any",
    "each", "every", "few", "more", "most", "other", "some", "such",
    "into", "through", "during", "before", "after", "above", "below",
    "about", "up", "out", "off", "over", "under", "again", "just", "very",
    "also", "s", "re", "t", "don", "ve", "ll", "d", "m", "https",
}


def fetch_feed(url: str, timeout: int = 15) -> str:
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "truth-dashboard/1.0"})
    resp.raise_for_status()
    return resp.text


def fetch_all_pages(base_url: str, max_pages: int) -> list[dict]:
    """Can technically fetch up to max_pages pages, Truth RSS does not include pages though. """
    posts = []
    seen_ids = set()

    for page in range(1, max_pages + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        print(f"  Fetching page {page}...", end=" ", flush=True)
        try:
            xml_text = fetch_feed(url)
        except requests.RequestException as e:
            print(f"failed ({e})")
            break

        page_posts = parse_feed(xml_text)
        if not page_posts:
            print("empty, stopping.")
            break

        new = [p for p in page_posts if p["id"] not in seen_ids]
        if not new:
            print("no new posts, stopping.")
            break

        for p in new:
            seen_ids.add(p["id"])
        posts.extend(new)
        print(f"{len(new)} posts (total: {len(posts)})")

        if len(new) < len(page_posts):
            # partial overlap — probably last page
            break

    return posts


def _post_to_json(post: dict) -> dict:
    """Make a post JSON-serialisable, converting datetime to ISO string."""
    p = post.copy()
    if isinstance(p.get("pub_date"), datetime):
        p["pub_date"] = p["pub_date"].isoformat()
    return p


def _post_from_json(p: dict) -> dict:
    """Restore a post loaded from JSON, parsing ISO string to datetime."""
    p = p.copy()
    raw = p.get("pub_date")
    if isinstance(raw, str):
        try:
            p["pub_date"] = datetime.fromisoformat(raw)
        except ValueError:
            p["pub_date"] = None
    return p


def load_store(path: str) -> dict[str, dict]:
    """Return {id: post} dict from the JSON store, or {} if it doesn't exist."""
    store_path = Path(path)
    if not store_path.exists():
        return {}
    with store_path.open(encoding="utf-8") as f:
        raw = json.load(f)
    return {p["id"]: _post_from_json(p) for p in raw}


def save_store(path: str, posts_by_id: dict[str, dict]) -> None:
    """Write all posts to the JSON store, sorted newest-first."""
    posts = sorted(
        posts_by_id.values(),
        key=lambda p: p["pub_date"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    with open(path, "w", encoding="utf-8") as f:
        json.dump([_post_to_json(p) for p in posts], f, ensure_ascii=False, indent=2)


def merge_into_store(store: dict[str, dict], new_posts: list[dict]) -> int:
    """Add new posts to store, skip duplicates. Returns count of truly new posts."""
    added = 0
    for p in new_posts:
        if p["id"] not in store:
            store[p["id"]] = p
            added += 1
    return added


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text).strip()


def extract_urls(html_text: str) -> list[str]:
    return re.findall(r'href=["\']([^"\']+)["\']', html_text)


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    ns = {"truth": "https://truthsocial.com/ns"}
    posts = []

    for item in root.findall(".//item"):
        def tag(name, default=""):
            el = item.find(name)
            return (el.text or "").strip() if el is not None else default

        description_raw = tag("description")
        plain_text = strip_html(description_raw)
        urls = extract_urls(description_raw)

        pub_date_str = tag("pubDate")
        try:
            pub_date = parsedate_to_datetime(pub_date_str)
            pub_date = pub_date.astimezone(timezone.utc)
        except Exception:
            pub_date = None

        original_url = tag("truth:originalUrl") or (
            item.find("truth:originalUrl", ns).text
            if item.find("truth:originalUrl", ns) is not None else ""
        )

        posts.append({
            "id": tag("guid") or tag("link"),
            "text": plain_text,
            "html": description_raw,
            "urls": urls,
            "pub_date": pub_date,
            "link": tag("link"),
            "original_url": original_url,
        })

    return posts


def _parse_count(raw: str) -> int | None:
    """Parse '785', '1.1k', '4.03k', '1.2M' → int."""
    raw = raw.strip().lower().replace(",", "")
    try:
        if raw.endswith("k"):
            return int(float(raw[:-1]) * 1_000)
        if raw.endswith("m"):
            return int(float(raw[:-1]) * 1_000_000)
        return int(raw)
    except ValueError:
        return None


def scrape_engagement(original_url: str, page) -> dict:
    """
    Open a Truth Social post page with an already-running Playwright page object
    and return {likes, retruths, replies}.  Any missing value is stored as None.
    """
    try:
        page.goto(original_url, wait_until="networkidle", timeout=20000)
    except Exception:
        return {"likes": None, "retruths": None, "replies": None}

    text = page.inner_text("body")

    # The rendered text contains patterns like:
    #   "785 replies"   (inline)
    #   "1.1k\nReTruths"  or  "1.1k ReTruths"  (number then label)
    #   "4.03k\nLikes"
    # Normalise to a single string and extract with regex.
    flat = " ".join(text.split())

    def find(pattern):
        m = re.search(pattern, flat, re.IGNORECASE)
        if m:
            return _parse_count(m.group(1))
        return None

    return {
        "likes":    find(r"([\d.,]+[kKmM]?)\s*likes"),
        "retruths": find(r"([\d.,]+[kKmM]?)\s*retruths"),
        "replies":  find(r"([\d.,]+[kKmM]?)\s*replies"),
    }


def enrich_posts(store: dict[str, dict], *, refresh: bool = False) -> int:
    """
    For every post with an original_url, scrape engagement counts if not yet present
    (or if refresh=True).  Updates posts in-place.  Returns number of posts scraped.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Error: playwright not installed. Run: pip install playwright && python -m playwright install chromium",
              file=sys.stderr)
        return 0

    to_scrape = [
        p for p in store.values()
        if p.get("original_url") and (refresh or p.get("likes") is None)
    ]
    if not to_scrape:
        print("  No posts need enriching.")
        return 0

    print(f"  Scraping engagement for {len(to_scrape)} posts (this may take a while)...")
    scraped = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ))
        for i, post in enumerate(to_scrape, 1):
            counts = scrape_engagement(post["original_url"], page)
            post.update(counts)
            scraped += 1
            likes = counts["likes"] if counts["likes"] is not None else "?"
            retruths = counts["retruths"] if counts["retruths"] is not None else "?"
            replies = counts["replies"] if counts["replies"] is not None else "?"
            print(f"  [{i}/{len(to_scrape)}] {post['original_url'].split('/')[-1]}"
                  f"  likes={likes}  retruths={retruths}  replies={replies}")
        browser.close()

    return scraped


def word_tokens(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z']+", text.lower())
    return [w.strip("'") for w in words if len(w) > 2 and w not in STOPWORDS]


def caps_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def compute_stats(posts: list[dict]) -> dict:
    non_empty = [p for p in posts if p["text"].strip()]
    total = len(posts)
    empty = total - len(non_empty)

    # Date range
    dates = [p["pub_date"] for p in posts if p["pub_date"]]
    dates_sorted = sorted(dates)
    date_min = dates_sorted[0] if dates_sorted else None
    date_max = dates_sorted[-1] if dates_sorted else None

    # Posts per day
    posts_per_day: dict[str, int] = defaultdict(int)
    posts_per_hour: dict[int, int] = defaultdict(int)
    posts_per_weekday: dict[int, int] = defaultdict(int)
    for p in posts:
        if p["pub_date"]:
            day_key = p["pub_date"].strftime("%Y-%m-%d")
            posts_per_day[day_key] += 1
            posts_per_hour[p["pub_date"].hour] += 1
            posts_per_weekday[p["pub_date"].weekday()] += 1

    # Word counts
    all_words: list[str] = []
    word_counts_per_post: list[int] = []
    char_counts_per_post: list[int] = []
    caps_ratios: list[float] = []

    for p in non_empty:
        words = word_tokens(p["text"])
        all_words.extend(words)
        word_counts_per_post.append(len(p["text"].split()))
        char_counts_per_post.append(len(p["text"]))
        caps_ratios.append(caps_ratio(p["text"]))

    top_words = Counter(all_words).most_common(30)

    # Links / domains
    all_urls: list[str] = []
    for p in posts:
        for u in p["urls"]:
            if not u.startswith("https://www.trumpstruth.org") and not u.startswith("https://truthsocial.com"):
                all_urls.append(u)

    domains = [urlparse(u).netloc.replace("www.", "") for u in all_urls if urlparse(u).netloc]
    top_domains = Counter(domains).most_common(15)
    posts_with_links = sum(1 for p in posts if any(
        not u.startswith("https://www.trumpstruth.org") and not u.startswith("https://truthsocial.com")
        for u in p["urls"]
    ))

    # CAPS stats
    avg_caps = (sum(caps_ratios) / len(caps_ratios) * 100) if caps_ratios else 0
    all_caps_posts = sum(1 for r in caps_ratios if r > 0.5)

    # Posts per day sorted for chart
    day_labels = sorted(posts_per_day.keys())
    day_values = [posts_per_day[d] for d in day_labels]

    avg_posts_per_day = (
        total / max(1, (date_max - date_min).days + 1) if date_min and date_max else 0
    )

    def avg(lst):
        return sum(lst) / len(lst) if lst else 0

    # Engagement
    enriched = [p for p in posts if p.get("likes") is not None]
    avg_likes    = round(avg([p["likes"]    for p in enriched]), 0) if enriched else None
    avg_retruths = round(avg([p["retruths"] for p in enriched if p.get("retruths") is not None]), 0) if enriched else None
    avg_replies  = round(avg([p["replies"]  for p in enriched if p.get("replies")  is not None]), 0) if enriched else None

    # Top 10 posts by likes
    top_by_likes = sorted(enriched, key=lambda p: p.get("likes") or 0, reverse=True)[:10]

    return {
        "total": total,
        "empty": empty,
        "non_empty": len(non_empty),
        "posts_with_links": posts_with_links,
        "date_min": date_min.strftime("%Y-%m-%d %H:%M UTC") if date_min else "N/A",
        "date_max": date_max.strftime("%Y-%m-%d %H:%M UTC") if date_max else "N/A",
        "avg_posts_per_day": round(avg_posts_per_day, 1),
        "top_words": top_words,
        "top_domains": top_domains,
        "avg_words": round(avg(word_counts_per_post), 1),
        "avg_chars": round(avg(char_counts_per_post), 1),
        "avg_caps_pct": round(avg_caps, 1),
        "all_caps_posts": all_caps_posts,
        "day_labels": day_labels,
        "day_values": day_values,
        "hourly": [posts_per_hour.get(h, 0) for h in range(24)],
        "weekday": [posts_per_weekday.get(d, 0) for d in range(7)],
        "word_count_hist": word_counts_per_post,
        "enriched_count": len(enriched),
        "avg_likes": int(avg_likes) if avg_likes is not None else None,
        "avg_retruths": int(avg_retruths) if avg_retruths is not None else None,
        "avg_replies": int(avg_replies) if avg_replies is not None else None,
        "top_by_likes": top_by_likes,
    }


# ─── HTML generation ──────────────────────────────────────────────────────────

def word_count_buckets(counts: list[int]) -> tuple[list[str], list[int]]:
    buckets = [
        (0, 10), (10, 25), (25, 50), (50, 100),
        (100, 200), (200, 400), (400, 10000),
    ]
    labels = ["1-10", "11-25", "26-50", "51-100", "101-200", "201-400", "400+"]
    values = []
    for lo, hi in buckets:
        values.append(sum(1 for c in counts if lo < c <= hi))
    return labels, values


WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

CHART_COLORS = [
    "#e74c3c", "#e67e22", "#f1c40f", "#2ecc71", "#1abc9c",
    "#3498db", "#9b59b6", "#e91e63", "#ff5722", "#607d8b",
    "#00bcd4", "#8bc34a", "#ff9800", "#795548", "#9e9e9e",
]


def make_html(stats: dict, generated_at: str) -> str:
    wc_labels, wc_values = word_count_buckets(stats["word_count_hist"])

    # Colour gradient for top-words bar
    word_labels = [w for w, _ in stats["top_words"]]
    word_values = [c for _, c in stats["top_words"]]
    word_colors = [CHART_COLORS[i % len(CHART_COLORS)] for i in range(len(word_labels))]

    domain_labels = [d for d, _ in stats["top_domains"]]
    domain_values = [c for _, c in stats["top_domains"]]
    domain_colors = [CHART_COLORS[i % len(CHART_COLORS)] for i in range(len(domain_labels))]

    def js(v):
        return json.dumps(v)

    # Stat card helper
    def card(value, label, sub=""):
        sub_html = f'<div class="card-sub">{sub}</div>' if sub else ""
        return f"""
        <div class="stat-card">
            <div class="card-value">{value}</div>
            <div class="card-label">{label}</div>
            {sub_html}
        </div>"""

    def fmt_num(n):
        if n is None: return "N/A"
        if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
        if n >= 1_000: return f"{n/1_000:.1f}k"
        return str(n)

    engagement_cards = ""
    if stats["enriched_count"]:
        engagement_cards = (
            card(fmt_num(stats["avg_likes"]), "Avg Likes",
                 f"over {stats['enriched_count']} enriched posts")
            + card(fmt_num(stats["avg_retruths"]), "Avg ReTruths")
            + card(fmt_num(stats["avg_replies"]), "Avg Replies")
        )

    cards_html = (
        card(stats["total"], "Total Posts", f"{stats['date_min']} → {stats['date_max']}")
        + card(stats["avg_posts_per_day"], "Avg Posts / Day")
        + card(stats["non_empty"], "Posts with Text", f"{stats['empty']} media-only")
        + card(f"{stats['posts_with_links']}", "Posts with Links",
               f"{round(stats['posts_with_links']/max(1,stats['total'])*100)}% of all posts")
        + card(f"{stats['avg_words']}", "Avg Word Count")
        + card(f"{stats['avg_caps_pct']}%", "Avg CAPS Ratio",
               f"{stats['all_caps_posts']} posts >50% caps")
        + engagement_cards
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trump's Truth Social — Statistics Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0f0f1a;
    color: #e0e0e0;
    min-height: 100vh;
    padding: 24px;
  }}
  h1 {{
    text-align: center;
    font-size: 1.9rem;
    font-weight: 700;
    letter-spacing: -0.5px;
    margin-bottom: 4px;
    background: linear-gradient(135deg, #e74c3c, #e67e22);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }}
  .subtitle {{
    text-align: center;
    color: #888;
    font-size: 0.85rem;
    margin-bottom: 28px;
  }}
  .cards {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 14px;
    margin-bottom: 28px;
  }}
  .stat-card {{
    background: #1a1a2e;
    border: 1px solid #2a2a44;
    border-radius: 12px;
    padding: 18px 16px;
    text-align: center;
  }}
  .card-value {{
    font-size: 2rem;
    font-weight: 700;
    color: #e74c3c;
    line-height: 1.1;
  }}
  .card-label {{
    font-size: 0.78rem;
    color: #aaa;
    margin-top: 4px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  .card-sub {{
    font-size: 0.72rem;
    color: #666;
    margin-top: 6px;
  }}
  .charts-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(480px, 1fr));
    gap: 20px;
  }}
  .chart-box {{
    background: #1a1a2e;
    border: 1px solid #2a2a44;
    border-radius: 12px;
    padding: 20px;
  }}
  .chart-box.full-width {{
    grid-column: 1 / -1;
  }}
  .chart-box h2 {{
    font-size: 0.9rem;
    color: #ccc;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 16px;
    border-bottom: 1px solid #2a2a44;
    padding-bottom: 10px;
  }}
  canvas {{ max-width: 100%; }}
  footer {{
    text-align: center;
    color: #444;
    font-size: 0.75rem;
    margin-top: 32px;
  }}
</style>
</head>
<body>
<h1>Trump's Truth Social</h1>
<div class="subtitle">Statistics Dashboard &mdash; generated {generated_at} &mdash; data via trumpstruth.org</div>

<div class="cards">{cards_html}</div>

<div class="charts-grid">

  <div class="chart-box full-width">
    <h2>Posts per Day</h2>
    <canvas id="chartDaily"></canvas>
  </div>

  <div class="chart-box">
    <h2>Posts by Hour of Day (UTC)</h2>
    <canvas id="chartHourly"></canvas>
  </div>

  <div class="chart-box">
    <h2>Posts by Day of Week</h2>
    <canvas id="chartWeekday"></canvas>
  </div>

  <div class="chart-box full-width">
    <h2>Top 30 Words</h2>
    <canvas id="chartWords"></canvas>
  </div>

  <div class="chart-box">
    <h2>Post Length Distribution (words)</h2>
    <canvas id="chartLength"></canvas>
  </div>

  <div class="chart-box">
    <h2>Top Linked Domains</h2>
    <canvas id="chartDomains"></canvas>
  </div>

{"" if not stats["enriched_count"] else f"""
  <div class="chart-box full-width">
    <h2>Top 10 Posts by Likes</h2>
    <canvas id="chartTopLikes"></canvas>
  </div>

  <div class="chart-box full-width">
    <h2>Engagement per Post (Likes / ReTruths / Replies)</h2>
    <canvas id="chartEngagement"></canvas>
  </div>
"""}

</div>

<footer>Source: <a href="https://www.trumpstruth.org/feed" style="color:#555">trumpstruth.org/feed</a></footer>

<script>
Chart.defaults.color = '#888';
Chart.defaults.borderColor = '#2a2a44';

const darkPlugin = {{
  id: 'darkBg',
  beforeDraw(chart) {{
    const ctx = chart.ctx;
    ctx.save();
    ctx.globalCompositeOperation = 'destination-over';
    ctx.fillStyle = '#1a1a2e';
    ctx.fillRect(0, 0, chart.width, chart.height);
    ctx.restore();
  }}
}};

// Daily
new Chart(document.getElementById('chartDaily'), {{
  type: 'bar',
  data: {{
    labels: {js(stats["day_labels"])},
    datasets: [{{ label: 'Posts', data: {js(stats["day_values"])},
      backgroundColor: '#e74c3c99', borderColor: '#e74c3c', borderWidth: 1 }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ ticks: {{ maxTicksLimit: 20 }} }} }}
  }},
  plugins: [darkPlugin]
}});

// Hourly
new Chart(document.getElementById('chartHourly'), {{
  type: 'bar',
  data: {{
    labels: Array.from({{length:24}}, (_,i) => i + ':00'),
    datasets: [{{ label: 'Posts', data: {js(stats["hourly"])},
      backgroundColor: '#3498db99', borderColor: '#3498db', borderWidth: 1 }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ display: false }} }}
  }},
  plugins: [darkPlugin]
}});

// Weekday
new Chart(document.getElementById('chartWeekday'), {{
  type: 'bar',
  data: {{
    labels: {js(WEEKDAY_NAMES)},
    datasets: [{{ label: 'Posts', data: {js(stats["weekday"])},
      backgroundColor: '#2ecc7199', borderColor: '#2ecc71', borderWidth: 1 }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ display: false }} }}
  }},
  plugins: [darkPlugin]
}});

// Top words
new Chart(document.getElementById('chartWords'), {{
  type: 'bar',
  data: {{
    labels: {js(word_labels)},
    datasets: [{{ label: 'Count', data: {js(word_values)},
      backgroundColor: {js(word_colors)}, borderWidth: 0 }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ y: {{ beginAtZero: true }} }}
  }},
  plugins: [darkPlugin]
}});

// Word length histogram
new Chart(document.getElementById('chartLength'), {{
  type: 'bar',
  data: {{
    labels: {js(wc_labels)},
    datasets: [{{ label: 'Posts', data: {js(wc_values)},
      backgroundColor: '#9b59b699', borderColor: '#9b59b6', borderWidth: 1 }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ display: false }} }}
  }},
  plugins: [darkPlugin]
}});

// Top domains
new Chart(document.getElementById('chartDomains'), {{
  type: 'bar',
  data: {{
    labels: {js(domain_labels)},
    datasets: [{{ label: 'Links', data: {js(domain_values)},
      backgroundColor: {js(domain_colors)}, borderWidth: 0 }}]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ beginAtZero: true }} }}
  }},
  plugins: [darkPlugin]
}});
{"" if not stats["enriched_count"] else f"""
// Top 10 posts by likes
(function() {{
  const top = {js([ {"label": (p["text"] or "")[:40].strip() + "…", "likes": p.get("likes") or 0, "retruths": p.get("retruths") or 0, "replies": p.get("replies") or 0} for p in stats["top_by_likes"] ])};
  new Chart(document.getElementById('chartTopLikes'), {{
    type: 'bar',
    data: {{
      labels: top.map(p => p.label),
      datasets: [
        {{ label: 'Likes',    data: top.map(p => p.likes),    backgroundColor: '#e74c3cbb' }},
        {{ label: 'ReTruths', data: top.map(p => p.retruths), backgroundColor: '#3498dbbb' }},
        {{ label: 'Replies',  data: top.map(p => p.replies),  backgroundColor: '#2ecc71bb' }},
      ]
    }},
    options: {{
      responsive: true,
      scales: {{ x: {{ ticks: {{ maxRotation: 30 }} }} }}
    }},
    plugins: [darkPlugin]
  }});
}})();

// Engagement timeline — likes per post over time
(function() {{
  const pts = {js(sorted([ {"x": p["pub_date"].strftime("%Y-%m-%d") if p.get("pub_date") else "", "likes": p.get("likes") or 0, "retruths": p.get("retruths") or 0, "replies": p.get("replies") or 0} for p in stats["top_by_likes"][:50] ], key=lambda x: x["x"]))};
  new Chart(document.getElementById('chartEngagement'), {{
    type: 'bar',
    data: {{
      labels: pts.map(p => p.x),
      datasets: [
        {{ label: 'Likes',    data: pts.map(p => p.likes),    backgroundColor: '#e74c3cbb' }},
        {{ label: 'ReTruths', data: pts.map(p => p.retruths), backgroundColor: '#3498dbbb' }},
        {{ label: 'Replies',  data: pts.map(p => p.replies),  backgroundColor: '#2ecc71bb' }},
      ]
    }},
    options: {{ responsive: true }},
    plugins: [darkPlugin]
  }});
}})();
"""}
</script>
</body>
</html>
"""


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch Trump's Truth Social RSS feed, persist posts, and generate an HTML dashboard."
    )
    parser.add_argument("--output", "-o", default="dashboard.html",
                        help="Output HTML file (default: dashboard.html)")
    parser.add_argument("--store", "-s", default=DEFAULT_STORE,
                        help=f"JSON file to accumulate posts in (default: {DEFAULT_STORE})")
    parser.add_argument("--pages", "-p", type=int, default=5,
                        help="Max feed pages to fetch per run (default: 5)")
    parser.add_argument("--feed-url", default=DEFAULT_FEED_URL,
                        help=f"RSS feed URL (default: {DEFAULT_FEED_URL})")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip fetching; rebuild dashboard from stored posts only")
    parser.add_argument("--enrich", action="store_true",
                        help="Scrape engagement counts (likes/retruths/replies) from Truth Social")
    parser.add_argument("--enrich-refresh", action="store_true",
                        help="Re-scrape engagement even for posts already enriched")
    args = parser.parse_args()

    # Load existing store
    store = load_store(args.store)
    print(f"Store '{args.store}': {len(store)} existing posts.")

    if not args.no_fetch:
        print(f"Fetching up to {args.pages} page(s) from {args.feed_url}")
        fetched = fetch_all_pages(args.feed_url, args.pages)
        added = merge_into_store(store, fetched)
        print(f"  {added} new post(s) added (store now: {len(store)}).")
        save_store(args.store, store)
        print(f"  Saved to '{args.store}'.")
    else:
        print("Skipping fetch (--no-fetch).")

    if args.enrich or args.enrich_refresh:
        print("Enriching posts with engagement data from Truth Social...")
        scraped = enrich_posts(store, refresh=args.enrich_refresh)
        if scraped:
            save_store(args.store, store)
            print(f"  Saved {scraped} enriched post(s) to '{args.store}'.")

    all_posts = list(store.values())
    if not all_posts:
        print("No posts in store. Exiting.", file=sys.stderr)
        sys.exit(1)

    print(f"\nComputing statistics over {len(all_posts)} posts...")
    stats = compute_stats(all_posts)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html_out = make_html(stats, generated_at)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"Dashboard written to: {args.output}")
    print(f"\nQuick summary:")
    print(f"  Posts:            {stats['total']} ({stats['date_min']} → {stats['date_max']})")
    print(f"  Avg posts/day:    {stats['avg_posts_per_day']}")
    print(f"  Empty posts:      {stats['empty']}")
    print(f"  Posts with links: {stats['posts_with_links']}")
    print(f"  Avg word count:   {stats['avg_words']}")
    print(f"  Avg CAPS ratio:   {stats['avg_caps_pct']}%")
    if stats["top_words"]:
        top5 = ", ".join(w for w, _ in stats["top_words"][:5])
        print(f"  Top 5 words:      {top5}")


if __name__ == "__main__":
    main()
