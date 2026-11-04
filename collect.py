"""Stage 1: collect RSS articles, dedupe, group same-story coverage, write to the Sheet.

Tab: NEWS DATABASE
Written by this script:  A ID | B Date | C Category | D Title | E Source | F URL | G Summary
                         K Story ID | L Outlets
Never touched (editorial review fills these): H Verdict | I Reason | J Score
"""
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import gspread
import requests

TAB = "NEWS DATABASE"
FEEDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feeds.json")
TZ = ZoneInfo("Asia/Kolkata")

MAX_AGE_HOURS = 48         # ignore articles published longer ago than this
MAX_PER_FEED = 40          # newest N items read per feed
CHECK_LAST_ROWS = 800      # how many recent sheet rows to compare against
REPOST_SIMILARITY = 0.90   # same outlet, near-identical title = repost, skipped
STORY_MIN_SHARED = 3       # same story across outlets: min shared keywords...
STORY_MIN_OVERLAP = 0.60   # ...and this share of the shorter title's keywords
STORY_TITLE_RATIO = 0.80   # ...or an overall title similarity this high

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

STOPWORDS = set("""a an the and or but of to in on at for with from by as is are was were be been
it its this that these those new how what why when who will would can could may might after before
over under into out up down about more most less than then also just not no yes you your we our they
their he she his her has have had do does did says said say get gets got""".split())

MUSIC_RE = re.compile(
    r"\b(music|song|songs|album|albums|singer|singers|singing|concert|concerts|gig|gigs|band|bands|"
    r"rapper|rappers|rap|hip.?hop|lyric|lyrics|playlist|spotify|world tour|live show|dj|djs|composer|"
    r"soundtrack|melody|vocalist|music video|coke studio|sunburn|lollapalooza|ep|single)\b",
    re.I,
)


def clean_url(url):
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", ""))


def clean_text(text, limit=300):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()[:limit]


def norm_title(title):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", title.lower())).strip()


def keywords(title):
    words = set()
    for w in norm_title(title).split():
        if len(w) < 3 or w in STOPWORDS:
            continue
        words.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return words


def make_item(**kw):
    kw["norm"] = norm_title(kw["title"])
    kw["kw"] = keywords(kw["title"])
    return kw


def same_story(a, b):
    shared = a["kw"] & b["kw"]
    smaller = min(len(a["kw"]), len(b["kw"])) or 1
    if len(shared) >= STORY_MIN_SHARED and len(shared) / smaller >= STORY_MIN_OVERLAP:
        return True
    return SequenceMatcher(None, a["norm"], b["norm"]).ratio() >= STORY_TITLE_RATIO


def published_dt(entry):
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*stamp[:6], tzinfo=timezone.utc) if stamp else None


def fetch_entries(url):
    reply = requests.get(url, headers=HEADERS, timeout=20)
    reply.raise_for_status()
    return feedparser.parse(reply.content).entries


def collect_candidates(feeds, now):
    candidates, report = [], []
    for feed in feeds:
        try:
            entries = fetch_entries(feed["url"])
        except Exception as exc:
            report.append((feed["name"], f"FAILED: {str(exc)[:90]}", False))
            continue
        kept = 0
        for e in entries[:MAX_PER_FEED]:
            link = (e.get("link") or "").strip()
            title = clean_text(e.get("title", ""), 200)
            dt = published_dt(e)
            if not link or not title:
                continue
            if dt and (now - dt).total_seconds() / 3600 > MAX_AGE_HOURS:
                continue
            summary = clean_text(e.get("summary") or e.get("description") or "")
            if feed.get("filter") == "music" and not MUSIC_RE.search(f"{title} {summary}"):
                continue
            candidates.append(make_item(category=feed["category"], source=feed["name"], title=title,
                                        url=link, summary=summary, dt=dt or now))
            kept += 1
        report.append((feed["name"], f"ok, {kept} candidates from {len(entries)} items", True))
    return candidates, report


def load_existing(sheet):
    values = sheet.get_all_values()
    pool, last_row, max_id = [], 1, 0
    all_urls = set()
    for n, r in enumerate(values, start=1):
        if n == 1:
            continue
        r = list(r) + [""] * (12 - len(r))
        if not any(c.strip() for c in r[:7]):
            continue
        last_row = n
        m = re.fullmatch(r"N(\d+)", r[0].strip())
        max_id = max(max_id, int(m.group(1))) if m else max_id
        if r[5]:
            all_urls.add(clean_url(r[5]))
        pool.append(make_item(row=n, id=r[0], category=r[2], title=r[3], source=r[4], url=r[5],
                              story=r[10].strip() or r[0], outlets=r[11].strip(), is_new=False))
    return pool[-CHECK_LAST_ROWS:], last_row, max_id, all_urls


def process(candidates, pool, known_urls, max_id, last_row):
    """Dedupe, group, and return (new_items, pool). Mutates pool."""
    new_items = []
    for cand in sorted(candidates, key=lambda c: c["dt"]):
        key = clean_url(cand["url"])
        if key in known_urls:
            continue
        repost = any(p["source"] == cand["source"] and p["category"] == cand["category"]
                     and SequenceMatcher(None, p["norm"], cand["norm"]).ratio() >= REPOST_SIMILARITY
                     for p in pool)
        if repost:
            continue
        known_urls.add(key)
        max_id += 1
        cand["id"] = f"N{max_id:05d}"
        cand["row"] = last_row + len(new_items) + 1
        cand["story"] = next((p["story"] for p in reversed(pool)
                              if p["category"] == cand["category"] and p["source"] != cand["source"]
                              and same_story(cand, p)), cand["id"])
        cand["is_new"] = True
        pool.append(cand)
        new_items.append(cand)
    return new_items


def build_writes(new_items, pool):
    outlets = {}
    for p in pool:
        outlets.setdefault(p["story"], set()).add(p["source"])
    data = []
    if new_items:
        start, end = new_items[0]["row"], new_items[-1]["row"]
        data.append({"range": f"A{start}:G{end}", "values": [
            [c["id"], c["dt"].astimezone(TZ).strftime("%Y-%m-%d %H:%M"), c["category"], c["title"],
             c["source"], c["url"], c["summary"]] for c in new_items]})
        data.append({"range": f"K{start}:L{end}", "values": [
            [c["story"], len(outlets[c["story"]])] for c in new_items]})
    for p in pool:  # update Outlets on older rows when new coverage joins their story
        count = len(outlets[p["story"]])
        if not p["is_new"] and str(p["outlets"]) != str(count):
            data.append({"range": f"L{p['row']}", "values": [[count]]})
    return data


def main():
    with open(FEEDS_FILE, encoding="utf-8") as f:
        feeds = json.load(f)
    creds = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    sheet = gspread.service_account_from_dict(creds).open_by_key(os.environ["SHEET_ID"]).worksheet(TAB)

    now = datetime.now(timezone.utc)
    candidates, report = collect_candidates(feeds, now)
    for name, msg, _ in report:
        print(f"{name}: {msg}")
    if not any(ok for _, _, ok in report):
        print("No feed could be read.")
        sys.exit(1)

    pool, last_row, max_id, known_urls = load_existing(sheet)
    new_items = process(candidates, pool, known_urls, max_id, last_row)
    data = build_writes(new_items, pool)

    if new_items and new_items[-1]["row"] > sheet.row_count:
        sheet.add_rows(new_items[-1]["row"] - sheet.row_count + 200)
    if data:
        sheet.batch_update(data, value_input_option="RAW")
    failed = [n for n, _, ok in report if not ok]
    print(f"Done. {len(candidates)} candidates, {len(new_items)} new rows added."
          + (f" Feeds that failed: {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
