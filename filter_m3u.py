#!/usr/bin/env python3
"""
Downloads each M3U URL listed in m3u_urls.txt, strips any channel whose
display name contains (case-insensitive) a term from GLOBAL_EXCLUDE_LIST or
whose group-title / #EXTGRP matches (case-insensitive, exact) an entry in
CATEGORY_EXCLUDE_LIST, drops duplicate streams, and writes each source out
as its own file in output/ (one .m3u per line in m3u_urls.txt, not merged).

m3u_urls.txt format (one entry per line, blank lines and lines starting with
# are ignored):
    MyProvider = https://example.com/playlist1.m3u8
    https://example.com/playlist2.m3u8

If no "name = " prefix is given, the URL's filename (or its position) is
used both for logging and as the output filename.

Duplicate detection compares stream URLs with known tracking/session query
params removed (TRACKING_PARAMS). With DEDUPE_ACROSS_SOURCES = True the first
source listed keeps a shared stream and later sources lose it; set it to
False to dedupe within each source only.

Exits nonzero if any source fails to fetch. A failed source's previous
output file is deleted so stale data is never left behind.
"""
import os
import re
import sys
import urllib.request
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from exclude_list import GLOBAL_EXCLUDE_LIST
from category_exclude_list import CATEGORY_EXCLUDE_LIST

URLS_FILE = "m3u_urls.txt"
OUTPUT_DIR = "output"
DEDUPE_ACROSS_SOURCES = True

# Query params ignored when comparing stream URLs. Everything else is kept,
# since many providers identify the channel by query (play.php?id=123).
TRACKING_PARAMS = {
    "token", "session", "sessionid", "sid", "sig", "signature", "expires",
    "exp", "ts", "timestamp", "t", "nonce", "uid", "deviceid", "device_id",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ad", "ads", "adid", "ad_id", "gdpr", "us_privacy", "cb", "rnd",
}

URL_TVG_RE = re.compile(r'url-tvg="([^"]*)"')
GROUP_TITLE_RE = re.compile(r'group-title="([^"]*)"')
SAFE_NAME_RE = re.compile(r'[^A-Za-z0-9_-]+')
# Attribute values in quotes may contain commas; the name follows the first
# comma that is outside quotes.
EXTINF_NAME_RE = re.compile(r'^#EXTINF:(?:[^,"]|"[^"]*")*,(.*)$')
NAMED_URL_RE = re.compile(r'^([^\s=/:?&]+)\s*=\s*(\S+)$')

_EXCLUDE_TERMS = [t.casefold() for t in GLOBAL_EXCLUDE_LIST if t and t.strip()]
_EXCLUDE_CATEGORIES = {c.strip().casefold() for c in CATEGORY_EXCLUDE_LIST}


def load_urls(path):
    """Returns a list of (name, url) tuples."""
    entries = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            m = NAMED_URL_RE.match(line)
            if m:
                entries.append((m.group(1), m.group(2)))
            else:
                base = os.path.basename(urlsplit(line).path)
                fallback = os.path.splitext(base)[0] or f"playlist{len(entries)+1}"
                entries.append((fallback, line))
    return entries


def safe_filename(name):
    cleaned = SAFE_NAME_RE.sub("_", name).strip("_")
    return cleaned or "playlist"


def unique_filename(name, used):
    base = safe_filename(name)
    fname = base
    n = 2
    while fname.casefold() in used:
        fname = f"{base}_{n}"
        n += 1
    used.add(fname.casefold())
    return fname


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        # utf-8-sig strips a leading BOM so header detection works
        return resp.read().decode("utf-8-sig", errors="replace")


def parse_playlist(text):
    """
    Returns (items, tvg_url_or_None, malformed_count).

    items is a list of either:
        ("raw", line)                      -- stray non-channel line
        ("entry", extinf, tag_lines, url)  -- one channel

    Blank lines inside an entry are skipped. An #EXTINF with no stream URL
    (next non-tag line missing, or another #EXTINF reached first) is dropped
    and counted as malformed. The #EXTM3U header is consumed, not returned.
    """
    lines = text.splitlines()
    items = []
    malformed = 0
    tvg_url = None
    i = 0

    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].startswith("#EXTM3U"):
        m = URL_TVG_RE.search(lines[i])
        if m:
            tvg_url = m.group(1)
        i += 1

    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF"):
            extinf = line
            tags = []
            url = None
            i += 1
            while i < len(lines):
                cur = lines[i]
                if not cur.strip():
                    i += 1
                elif cur.startswith("#EXTINF"):
                    break
                elif cur.startswith("#"):
                    tags.append(cur)
                    i += 1
                else:
                    url = cur.strip()
                    i += 1
                    break
            if url is None:
                malformed += 1
            else:
                items.append(("entry", extinf, tags, url))
        else:
            if line.strip():
                items.append(("raw", line))
            i += 1

    return items, tvg_url, malformed


def entry_name(extinf):
    m = EXTINF_NAME_RE.match(extinf)
    return m.group(1) if m else extinf.rsplit(",", 1)[-1]


def entry_category(extinf, tags):
    m = GROUP_TITLE_RE.search(extinf)
    if m:
        return m.group(1)
    for tag in tags:
        if tag.startswith("#EXTGRP:"):
            return tag[len("#EXTGRP:"):]
    return None


def filter_items(items):
    """Returns (kept_items, removed_by_name, removed_by_category)."""
    kept = []
    by_name = 0
    by_category = 0
    for item in items:
        if item[0] != "entry":
            kept.append(item)
            continue
        _, extinf, tags, _url = item
        category = entry_category(extinf, tags)
        if category is not None and category.strip().casefold() in _EXCLUDE_CATEGORIES:
            by_category += 1
            continue
        name = entry_name(extinf).casefold()
        if any(term in name for term in _EXCLUDE_TERMS):
            by_name += 1
            continue
        kept.append(item)
    return kept, by_name, by_category


def normalize_url(url):
    """
    Drops the fragment and known tracking/session query params so the same
    stream served with different ad/session params compares equal. Other
    query params are kept (sorted). Only the host is lowercased; paths can
    be case-sensitive.
    """
    parts = urlsplit(url)
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.casefold() not in TRACKING_PARAMS
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path, urlencode(query), ""))


def dedupe_items(items, seen_urls):
    """
    Removes entries whose normalized stream URL is already in seen_urls
    (first occurrence wins). seen_urls is mutated in place.
    Returns (deduped_items, removed_count).
    """
    kept = []
    removed = 0
    for item in items:
        if item[0] != "entry":
            kept.append(item)
            continue
        key = normalize_url(item[3])
        if key in seen_urls:
            removed += 1
            continue
        seen_urls.add(key)
        kept.append(item)
    return kept, removed


def render(items, tvg_url=None):
    header = "#EXTM3U"  # upstream url-tvg intentionally not embedded
    out = [header]
    for item in items:
        if item[0] == "entry":
            _, extinf, tags, url = item
            out.append(extinf)
            out.extend(tags)
            out.append(url)
        else:
            out.append(item[1])
    return "\n".join(out) + "\n"


def main():
    if not os.path.exists(URLS_FILE):
        print(f"Error: {URLS_FILE} not found", file=sys.stderr)
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    entries = load_urls(URLS_FILE)
    if not entries:
        print(f"Error: no URLs found in {URLS_FILE}", file=sys.stderr)
        sys.exit(1)

    total_by_name = 0
    total_by_category = 0
    total_duplicates = 0
    total_malformed = 0
    fetched = 0
    failed = 0
    shared_seen = set()
    used_filenames = set()

    for name, url in entries:
        fname = unique_filename(name, used_filenames)
        out_path = os.path.join(OUTPUT_DIR, f"{fname}.m3u")

        try:
            raw = fetch(url)
        except Exception as e:
            print(f"[{name}] FAILED to fetch: {e}", file=sys.stderr)
            failed += 1
            if os.path.exists(out_path):
                os.remove(out_path)
                print(f"[{name}] removed stale {out_path}", file=sys.stderr)
            continue

        items, tvg_url, malformed = parse_playlist(raw)
        items, by_name, by_category = filter_items(items)
        seen = shared_seen if DEDUPE_ACROSS_SOURCES else set()
        items, duplicates = dedupe_items(items, seen)

        total_by_name += by_name
        total_by_category += by_category
        total_duplicates += duplicates
        total_malformed += malformed
        fetched += 1

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(render(items, tvg_url))

        channels = sum(1 for it in items if it[0] == "entry")
        print(f"[{name}] {by_name} removed by name, "
              f"{by_category} removed by category, "
              f"{duplicates} duplicate streams removed, "
              f"{malformed} malformed entries dropped, "
              f"{channels} channels kept -> {out_path}")

    total_removed = total_by_name + total_by_category
    print(f"Done. {total_removed} channels removed "
          f"({total_by_name} by name, {total_by_category} by category), "
          f"{total_duplicates} duplicate streams removed, "
          f"{total_malformed} malformed entries dropped "
          f"across {fetched}/{len(entries)} source(s).")

    if fetched == 0:
        print("Error: every playlist fetch failed, nothing to write.", file=sys.stderr)
        sys.exit(1)
    if failed:
        print(f"Error: {failed} source(s) failed to fetch.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
