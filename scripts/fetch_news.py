#!/usr/bin/env python3
"""
Fetches local open-weight model news from Reddit (r/LocalLLaMA), Hacker News
(Algolia API), and GitHub/Hugging Face release RSS feeds. Categorizes each
item into model/security/harness news via keyword heuristics (see
scripts/sources.json), and writes:

  - data/news.json            current feed, consumed by the site
  - data/archive/YYYY-MM-DD.json   today's snapshot, for the archive browser
  - data/archive/index.json   list of available archive dates (pruned)
  - feed.xml                  a combined RSS 2.0 feed of the current items

Designed to run unattended on a schedule (see .github/workflows/update-news.yml).
Any single source failing does not fail the whole run.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "news.json"
ARCHIVE_DIR = ROOT / "data" / "archive"
ARCHIVE_INDEX_PATH = ARCHIVE_DIR / "index.json"
HISTORY_PATH = ROOT / "data" / "history.json"
FEED_PATH = ROOT / "feed.xml"
CONFIG_PATH = Path(__file__).resolve().parent / "sources.json"

SITE_NAME = "Simple News"
SITE_URL = os.environ.get("SITE_URL", "https://twinkites.github.io/simple-llm-news/")

USER_AGENT = "SimpleNewsBot/1.0 (static news aggregator; contact: twinkites@proton.me)"
MAX_PER_SECTION = 12
LOOKBACK_DAYS = 10
ARCHIVE_RETENTION_DAYS = 90
HISTORY_DAYS = 30
GRAVITY = 1.8
RSS_ITEMS_PER_FEED = 3

CONFIG = json.loads(CONFIG_PATH.read_text())
HN_QUERIES = CONFIG["hn_queries"]
RSS_FEEDS = CONFIG["rss_feeds"]
KEYWORDS = CONFIG["keywords"]

ATOM_NS = "{http://www.w3.org/2005/Atom}"


def log(msg):
    print(f"[fetch_news] {msg}", file=sys.stderr)


def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _compile_keyword_patterns(keywords):
    # Word-boundary match, not a bare substring check - otherwise short
    # keywords like "rce" match inside ordinary words ("open-source"
    # contains "rce" as a substring: sou-RCE).
    return [re.compile(r"\b" + re.escape(kw) + r"\b") for kw in keywords]


_KEYWORD_PATTERNS = {cat: _compile_keyword_patterns(kws) for cat, kws in KEYWORDS.items()}


def categorize(title, snippet=""):
    text = f"{title} {snippet}".lower()
    for cat in ("security", "harness", "model"):
        for pattern in _KEYWORD_PATTERNS[cat]:
            if pattern.search(text):
                return cat
    return None


def get_reddit_token():
    """Application-only OAuth (client_credentials). Optional: without
    REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET env vars we fall back to the
    unauthenticated .json endpoint, which many cloud/CI IPs get blocked
    from. A free Reddit "script" app fixes that reliably."""
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    try:
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req = urllib.request.Request(
            "https://www.reddit.com/api/v1/access_token",
            data=b"grant_type=client_credentials",
            headers={
                "Authorization": f"Basic {creds}",
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get("access_token")
    except Exception as e:
        log(f"reddit oauth token failed: {e}")
        return None


def fetch_reddit():
    items = []
    token = get_reddit_token()
    base = "https://oauth.reddit.com" if token else "https://www.reddit.com"
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    for t in ("day", "week"):
        url = f"{base}/r/LocalLLaMA/top.json?limit=50&t={t}&raw_json=1"
        try:
            raw = http_get(url, headers=headers)
            data = json.loads(raw)
            for child in data.get("data", {}).get("children", []):
                d = child.get("data", {})
                title = d.get("title", "").strip()
                if not title or d.get("stickied"):
                    continue
                url_out = d.get("url_overridden_by_dest") or f"https://reddit.com{d.get('permalink', '')}"
                published = datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc).isoformat()
                items.append({
                    "title": title,
                    "url": url_out,
                    "source": "reddit",
                    "score": d.get("score", 0),
                    "published": published,
                    "category": categorize(title, d.get("selftext", "")[:300]),
                })
        except Exception as e:
            log(f"reddit fetch failed for t={t}: {e}")
    log(f"reddit: {len(items)} items")
    return items


def fetch_hn():
    items = []
    since = int(time.time()) - LOOKBACK_DAYS * 86400
    for q in HN_QUERIES:
        url = (
            "https://hn.algolia.com/api/v1/search?"
            + urllib.parse.urlencode({
                "query": q,
                "tags": "story",
                "numericFilters": f"created_at_i>{since}",
                "hitsPerPage": 20,
            })
        )
        try:
            raw = http_get(url)
            data = json.loads(raw)
            for hit in data.get("hits", []):
                title = (hit.get("title") or "").strip()
                if not title:
                    continue
                url_out = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                items.append({
                    "title": title,
                    "url": url_out,
                    "source": "hn",
                    "score": hit.get("points", 0) or 0,
                    "published": hit.get("created_at"),
                    "category": categorize(title),
                })
        except Exception as e:
            log(f"hn fetch failed for query={q!r}: {e}")
    log(f"hn: {len(items)} items")
    return items


def _parse_feed_datetime(text):
    if not text:
        return None
    text = text.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def fetch_rss():
    items = []
    for feed in RSS_FEEDS:
        url, label = feed["url"], feed["label"]
        try:
            raw = http_get(url)
            root = ET.fromstring(raw)

            # Atom feeds (GitHub releases.atom). Some projects (llama.cpp)
            # cut a release per commit, which would otherwise flood a
            # section with near-duplicate build numbers - keep only the
            # most recent few per feed.
            entries = root.findall(f"{ATOM_NS}entry")[:RSS_ITEMS_PER_FEED]
            if entries:
                for entry in entries:
                    title_el = entry.find(f"{ATOM_NS}title")
                    link_el = entry.find(f"{ATOM_NS}link")
                    updated_el = entry.find(f"{ATOM_NS}updated")
                    if updated_el is None:
                        updated_el = entry.find(f"{ATOM_NS}published")
                    title = (title_el.text or "").strip() if title_el is not None else ""
                    if not title:
                        continue
                    href = link_el.get("href") if link_el is not None else None
                    if not href:
                        continue
                    items.append({
                        "title": f"{label}: {title}",
                        "url": href,
                        "source": "rss",
                        "score": 0,
                        "published": _parse_feed_datetime(updated_el.text if updated_el is not None else None),
                        "category": categorize(title, label) or "harness",
                    })
                continue

            # RSS 2.0 feeds (Hugging Face blog)
            for item in root.findall(".//item")[:RSS_ITEMS_PER_FEED]:
                title_el = item.find("title")
                link_el = item.find("link")
                pub_el = item.find("pubDate")
                title = (title_el.text or "").strip() if title_el is not None else ""
                if not title:
                    continue
                href = (link_el.text or "").strip() if link_el is not None else None
                if not href:
                    continue
                items.append({
                    "title": title,
                    "url": href,
                    "source": "rss",
                    "score": 0,
                    "published": _parse_feed_datetime(pub_el.text if pub_el is not None else None),
                    "category": categorize(title, label) or "model",
                })
        except Exception as e:
            log(f"rss fetch failed for {label} ({url}): {e}")
    log(f"rss: {len(items)} items")
    return items


def dedupe(items):
    seen = set()
    out = []
    for item in items:
        key = item["url"].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def rank_score(item, now):
    """HN-style gravity ranking so fast-rising items outrank stale
    high-scorers. Items without a score (RSS/web) get a baseline of 1 so
    recency alone still ranks them; items without a timestamp are treated
    as maximally old rather than as ties with brand-new items."""
    published = item.get("published")
    if published:
        try:
            # HN's Algolia API uses a trailing "Z", which fromisoformat only
            # parses natively on Python 3.11+. Normalize it so this doesn't
            # silently degrade ranking on older interpreters (e.g. local dev).
            parsed = datetime.fromisoformat(published.replace("Z", "+00:00"))
            age_hours = max(0.0, (now - parsed).total_seconds() / 3600)
        except ValueError:
            age_hours = LOOKBACK_DAYS * 24
    else:
        age_hours = LOOKBACK_DAYS * 24
    score = (item.get("score") or 0) + 1
    return score / (age_hours + 2) ** GRAVITY


def build_sections(all_items):
    sections = {"model": [], "security": [], "harness": []}
    for item in all_items:
        cat = item.pop("category", None)
        if cat not in sections:
            continue
        sections[cat].append(item)

    now = datetime.now(timezone.utc)
    for cat, items in sections.items():
        items.sort(key=lambda i: rank_score(i, now), reverse=True)
        sections[cat] = items[:MAX_PER_SECTION]

    return sections


def write_archive(out):
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (ARCHIVE_DIR / f"{today}.json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")

    dates = sorted({p.stem for p in ARCHIVE_DIR.glob("*.json")}, reverse=True)
    cutoff = datetime.now(timezone.utc).timestamp() - ARCHIVE_RETENTION_DAYS * 86400
    kept = []
    for d in dates:
        try:
            ts = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if ts < cutoff:
            (ARCHIVE_DIR / f"{d}.json").unlink(missing_ok=True)
        else:
            kept.append(d)

    ARCHIVE_INDEX_PATH.write_text(json.dumps({"dates": kept}, indent=2) + "\n")
    log(f"archive: wrote {today}.json, {len(kept)} dates retained")
    return kept


def write_history(dates):
    """Per-day item counts by section, for the bar chart at the bottom of
    the page. Built from the archive snapshots that already exist, so
    there's no separate data source to keep in sync."""
    recent = sorted(dates)[-HISTORY_DAYS:]
    days = []
    for d in recent:
        path = ARCHIVE_DIR / f"{d}.json"
        try:
            snapshot = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log(f"history: could not read {path}: {e}")
            continue
        counts = {cat: len(items) for cat, items in snapshot.get("sections", {}).items()}
        days.append({"date": d, "counts": counts})

    HISTORY_PATH.write_text(json.dumps({"days": days}, indent=2) + "\n")
    log(f"wrote {HISTORY_PATH} ({len(days)} days)")


def write_feed(sections):
    now = format_datetime(datetime.now(timezone.utc))
    items_xml = []
    for cat, items in sections.items():
        for item in items:
            pub = item.get("published")
            pub_rfc822 = now
            if pub:
                try:
                    pub_rfc822 = format_datetime(datetime.fromisoformat(pub))
                except ValueError:
                    pass
            items_xml.append(f"""    <item>
      <title>{escape(item['title'])}</title>
      <link>{escape(item['url'])}</link>
      <guid isPermaLink="false">{escape(item['url'])}</guid>
      <category>{escape(cat)}</category>
      <pubDate>{pub_rfc822}</pubDate>
      <source>{escape(item['source'])}</source>
    </item>""")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{escape(SITE_NAME)}</title>
    <link>{escape(SITE_URL)}</link>
    <description>Local open-weight model news, security news, and harness news.</description>
    <lastBuildDate>{now}</lastBuildDate>
{chr(10).join(items_xml)}
  </channel>
</rss>
"""
    FEED_PATH.write_text(xml)
    log(f"wrote {FEED_PATH}")


def carry_forward_truthiness(sections):
    """Items that persist across runs (common - top items don't disappear
    in 6 hours) keep their previous truthiness label instead of paying for
    a fresh, identical LLM call in evaluate_truthiness.py. Only applies
    when this run's news.json still exists from a prior run."""
    if not OUT_PATH.exists():
        return
    try:
        previous = json.loads(OUT_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log(f"could not read previous {OUT_PATH} for truthiness carry-forward: {e}")
        return

    by_url = {
        item["url"]: item["truthiness"]
        for items in previous.get("sections", {}).values()
        for item in items
        if "truthiness" in item
    }
    carried = 0
    for items in sections.values():
        for item in items:
            if item["url"] in by_url:
                item["truthiness"] = by_url[item["url"]]
                carried += 1
    log(f"carried forward {carried} truthiness labels from previous run")


def main():
    all_items = []
    all_items += fetch_reddit()
    all_items += fetch_hn()
    all_items += fetch_rss()

    all_items = dedupe(all_items)
    sections = build_sections(all_items)

    for cat, items in sections.items():
        log(f"section {cat}: {len(items)} items after filtering/truncation")

    total = sum(len(v) for v in sections.values())
    if total == 0:
        log("WARNING: zero items across all sections, leaving previous output untouched")
        sys.exit(2)

    carry_forward_truthiness(sections)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sections": sections,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    log(f"wrote {OUT_PATH}")

    kept_dates = write_archive(out)
    write_history(kept_dates)
    write_feed(sections)


if __name__ == "__main__":
    main()
