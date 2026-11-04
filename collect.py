"""Stage 1: collect RSS articles, deduplicate individual articles, and group story coverage.

Google spreadsheet title is supplied via SHEET_ID.
Worksheet tab defaults to "NEWS DATABASE" and can be overridden with SHEET_TAB.

Columns:
A ID | B Status | C Date | D Category | E Title | F Source | G URL | H Summary
I Verdict | J Reason | K Score | L Story ID | M Outlets

Stage 1 writes A:H for new articles and L:M for story grouping.
It never writes to I:K (editorial fields).
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import gspread
import requests

BASE_DIR = Path(__file__).resolve().parent
FEEDS_FILE = BASE_DIR / "feeds.json"
SHEET_TAB = os.getenv("SHEET_TAB") or "NEWS DATABASE"
TZ = ZoneInfo("Asia/Kolkata")

MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "48"))
MAX_PER_FEED = int(os.getenv("MAX_PER_FEED", "40"))
REPOST_SIMILARITY = float(os.getenv("REPOST_SIMILARITY", "0.90"))
STORY_MIN_SHARED = int(os.getenv("STORY_MIN_SHARED", "3"))
STORY_MIN_OVERLAP = float(os.getenv("STORY_MIN_OVERLAP", "0.60"))
STORY_TITLE_RATIO = float(os.getenv("STORY_TITLE_RATIO", "0.80"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsroomRSSBot/1.0)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}

STOPWORDS = set("""a an the and or but of to in on at for with from by as is are was were be been
it its this that these those new how what why when who will would can could may might after before
over under into out up down about more most less than then also just not no yes you your we our they
their he she his her has have had do does did says said say get gets got""".split())

MUSIC_RE = re.compile(
    r"\b(music|song|songs|album|albums|singer|singers|singing|concert|concerts|gig|gigs|band|bands|"
    r"rapper|rappers|rap|hip.?hop|lyric|lyrics|playlist|spotify|world tour|live show|dj|djs|composer|"
    r"soundtrack|melody|vocalist|music video|coke studio|sunburn|lollapalooza|ep|single|"
    r"playback singer|record label|music festival|streaming)\b",
    re.I,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def clean_url(url: str) -> str:
    """Normalize URL for exact-article deduplication; ignore query strings/fragments."""
    try:
        p = urlsplit(url.strip())
        host = p.netloc.lower()
        path = re.sub(r"/+", "/", p.path).rstrip("/")
        return urlunsplit((p.scheme.lower(), host, path, "", ""))
    except Exception:
        return url.strip()


def clean_text(value: str, limit: int = 500) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return html.unescape(re.sub(r"\s+", " ", value)).strip()[:limit]


def norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", title.lower())).strip()


def keywords(title: str) -> set[str]:
    words = set()
    for word in norm_title(title).split():
        if len(word) < 3 or word in STOPWORDS:
            continue
        words.add(word[:-1] if word.endswith("s") and len(word) > 4 else word)
    return words


def make_item(**kwargs):
    kwargs["norm"] = norm_title(kwargs.get("title", ""))
    kwargs["kw"] = keywords(kwargs.get("title", ""))
    return kwargs


def story_similarity(a: dict, b: dict) -> float:
    """Return a rough title similarity score; 0 means no meaningful match."""
    shared = a["kw"] & b["kw"]
    smaller = min(len(a["kw"]), len(b["kw"])) or 1
    overlap = len(shared) / smaller
    ratio = SequenceMatcher(None, a["norm"], b["norm"]).ratio()
    if len(shared) >= STORY_MIN_SHARED and overlap >= STORY_MIN_OVERLAP:
        return max(ratio, overlap)
    if ratio >= STORY_TITLE_RATIO:
        return ratio
    return 0.0


def published_dt(entry):
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    if stamp:
        try:
            return datetime(*stamp[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
    return None


def fetch_entries(url: str):
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise ValueError(f"Feed could not be parsed: {getattr(parsed, 'bozo_exception', 'unknown error')}")
    return parsed.entries


def collect_candidates(feeds: list[dict], now: datetime):
    candidates, report = [], []
    for feed in feeds:
        name = feed.get("name", feed.get("url", "Unnamed feed"))
        try:
            entries = fetch_entries(feed["url"])
            kept = 0
            for entry in entries[:MAX_PER_FEED]:
                link = (entry.get("link") or "").strip()
                title = clean_text(entry.get("title", ""), 250)
                if not link or not title:
                    continue

                published = published_dt(entry)
                if published and (now - published).total_seconds() / 3600 > MAX_AGE_HOURS:
                    continue

                summary = clean_text(entry.get("summary") or entry.get("description") or "", 700)
                if feed.get("filter") == "music" and not MUSIC_RE.search(f"{title} {summary}"):
                    continue

                category = feed.get("category", "").strip()
                if category.lower() == "music":
                    category = "Indian Music"

                candidates.append(make_item(
                    category=category,
                    source=name,
                    title=title,
                    url=link,
                    summary=summary,
                    dt=published or now,
                ))
                kept += 1
            report.append((name, f"OK — {kept} candidates from {len(entries)} feed items", True))
        except Exception as exc:
            report.append((name, f"FAILED — {str(exc)[:180]}", False))
    return candidates, report


def load_existing(sheet):
    """Load all populated article rows so IDs, dedupe and outlet counts stay consistent."""
    values = sheet.get_all_values()
    pool = []
    known_urls = set()
    max_article_id = 0
    max_story_id = 0
    last_row = 1

    for row_number, raw in enumerate(values[1:], start=2):
        row = list(raw) + [""] * max(0, 13 - len(raw))
        if not any(str(cell).strip() for cell in row[:13]):
            continue

        last_row = row_number
        article_id = str(row[0]).strip()
        status = str(row[1]).strip().upper()
        category = str(row[3]).strip()
        title = str(row[4]).strip()
        source = str(row[5]).strip()
        url = str(row[6]).strip()
        story_id = str(row[11]).strip()
        outlet_count = str(row[12]).strip()

        article_match = re.fullmatch(r"N(\d+)", article_id)
        if article_match:
            max_article_id = max(max_article_id, int(article_match.group(1)))

        story_match = re.fullmatch(r"S(\d+)", story_id)
        if story_match:
            max_story_id = max(max_story_id, int(story_match.group(1)))

        if url:
            known_urls.add(clean_url(url))

        if title and source:
            pool.append(make_item(
                row=row_number,
                id=article_id,
                status=status,
                category=category,
                title=title,
                source=source,
                url=url,
                story=story_id,
                outlets=outlet_count,
                is_new=False,
            ))

    return pool, last_row, max_article_id, max_story_id, known_urls


def is_same_outlet_repost(candidate: dict, pool: list[dict]) -> bool:
    for old in pool:
        if old["source"].casefold() != candidate["source"].casefold():
            continue
        if old["category"].casefold() != candidate["category"].casefold():
            continue
        if SequenceMatcher(None, old["norm"], candidate["norm"]).ratio() >= REPOST_SIMILARITY:
            return True
    return False


def best_story_match(candidate: dict, pool: list[dict]):
    best_item, best_score = None, 0.0
    for old in pool:
        if old["category"].casefold() != candidate["category"].casefold():
            continue
        score = story_similarity(candidate, old)
        if score > best_score:
            best_item, best_score = old, score
    return best_item


def process_candidates(candidates, pool, known_urls, max_article_id, max_story_id, last_row):
    """Keep distinct articles; assign shared Story IDs to related coverage."""
    new_items = []

    # Process oldest first so each new article can join a group created earlier in this run.
    for candidate in sorted(candidates, key=lambda item: item["dt"]):
        url_key = clean_url(candidate["url"])
        if not url_key or url_key in known_urls:
            continue
        if is_same_outlet_repost(candidate, pool):
            continue

        known_urls.add(url_key)
        max_article_id += 1
        candidate["id"] = f"N{max_article_id:05d}"
        candidate["row"] = last_row + len(new_items) + 1
        candidate["is_new"] = True
        candidate["status"] = "NEW"

        match = best_story_match(candidate, pool)
        if match and match.get("story"):
            candidate["story"] = match["story"]
        else:
            max_story_id += 1
            candidate["story"] = f"S{max_story_id:05d}"

        candidate["outlets"] = ""
        pool.append(candidate)
        new_items.append(candidate)

    return new_items, pool


def build_updates(new_items, pool):
    """Write only new article fields plus Story ID/Outlets; never touch I:K."""
    if not new_items:
        # Existing groups may still have inconsistent counts; refresh counts below.
        pass

    sources_by_story = {}
    for item in pool:
        story_id = item.get("story", "")
        source = item.get("source", "").strip()
        if story_id and source:
            sources_by_story.setdefault(story_id, set()).add(source.casefold())

    updates = []
    for item in new_items:
        outlet_count = len(sources_by_story.get(item["story"], set()))
        local_date = item["dt"].astimezone(TZ).strftime("%Y-%m-%d %H:%M")
        row = item["row"]
        updates.append({
            "range": f"A{row}:H{row}",
            "values": [[
                item["id"], "NEW", local_date, item["category"], item["title"],
                item["source"], item["url"], item["summary"],
            ]],
        })
        updates.append({
            "range": f"L{row}:M{row}",
            "values": [[item["story"], outlet_count]],
        })

    # Refresh the outlet count for every existing article in a story group that changed,
    # and also repair missing/stale counts in the existing database.
    for item in pool:
        if item.get("is_new"):
            continue
        expected = len(sources_by_story.get(item.get("story", ""), set()))
        if str(item.get("outlets", "")).strip() != str(expected):
            updates.append({
                "range": f"M{item['row']}",
                "values": [[expected]],
            })

    return updates


def main():
    if not os.getenv("GOOGLE_CREDENTIALS"):
        raise RuntimeError("Missing GOOGLE_CREDENTIALS GitHub secret.")
    if not os.getenv("SHEET_ID"):
        raise RuntimeError("Missing SHEET_ID GitHub secret.")
    if not FEEDS_FILE.exists():
        raise RuntimeError(f"Missing feeds file: {FEEDS_FILE}")

    with FEEDS_FILE.open(encoding="utf-8") as handle:
        feeds = json.load(handle)

    credentials = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    client = gspread.service_account_from_dict(credentials)
    spreadsheet = client.open_by_key(os.environ["SHEET_ID"])
    sheet = spreadsheet.worksheet(SHEET_TAB)

    now = datetime.now(timezone.utc)
    candidates, report = collect_candidates(feeds, now)

    for name, message, ok in report:
        logging.info("%s: %s", name, message)

    successful_feeds = sum(1 for _, _, ok in report if ok)
    if successful_feeds == 0:
        logging.error("All RSS feeds failed. Sheet was not changed.")
        sys.exit(1)

    pool, last_row, max_article_id, max_story_id, known_urls = load_existing(sheet)
    new_items, pool = process_candidates(
        candidates, pool, known_urls, max_article_id, max_story_id, last_row
    )
    updates = build_updates(new_items, pool)

    if new_items and new_items[-1]["row"] > sheet.row_count:
        sheet.add_rows(new_items[-1]["row"] - sheet.row_count + 100)

    if updates:
        sheet.batch_update(updates, value_input_option="RAW")

    failed_feeds = [name for name, _, ok in report if not ok]
    logging.info(
        "Collection complete: %d feed candidates checked; %d new articles added; %d feed(s) failed.",
        len(candidates), len(new_items), len(failed_feeds)
    )
    if failed_feeds:
        logging.warning("Failed feeds: %s", ", ".join(failed_feeds))


if __name__ == "__main__":
    main()
