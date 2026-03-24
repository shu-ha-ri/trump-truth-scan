# truth-rss

CLI app to fetch Trump's Truth Social posts from [trumpstruth.org/feed](https://www.trumpstruth.org/feed), stores them locally, and generates a HTML statistics dashboard. Posts are fetched from RSS feed, engagement statistics are fetched through headless browser via Playwright.

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Usage

```bash
# Fetch new posts, scrape engagement, rebuild dashboard
python truth_dashboard.py --enrich

# Just fetch new posts (no engagement scraping)
python truth_dashboard.py

# Rebuild dashboard from stored data without any network calls
python truth_dashboard.py --no-fetch
```

Open `dashboard.html` in a browser to view the result.

## Optional: daily cron

```
0 8 * * * cd /path/to/truth-rss && python truth_dashboard.py --enrich
```

Posts get stored in `posts.json`. The RSS feed is limited to 100 posts, so running daily builds a historical record. Engagement counts (likes, ReTruths, replies) are scraped once per post and cached — reruns only hit new posts.
