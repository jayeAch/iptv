#!/usr/bin/env python3
"""
Downloads each M3U URL listed in urls.txt, strips any channel whose
#EXTINF line matches a word/substring in GLOBAL_EXCLUDE_LIST or whose
group-title exactly matches an entry in CATEGORY_EXCLUDE_LIST, and merges
all of them into a single output/combined.m3u.

urls.txt format (one entry per line, blank lines and lines starting with
# are ignored):
    MyProvider = https://example.com/playlist1.m3u8
    https://example.com/playlist2.m3u8

If no "name = " prefix is given, the URL's filename (or its position)
is used only for logging (the merged file has no per-source filenames).
"""
import os
import re
import sys
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from exclude_list import GLOBAL_EXCLUDE_LIST
from category_exclude_list import CATEGORY_EXCLUDE_LIST

URLS_FILE = "urls.txt"
OUTPUT_DIR = "output"
COMBINED_OUTPUT = os.path.join(OUTPUT_DIR, "combined.m3u")

URL_TVG_RE = re.compile(r'url-tvg="([^"]*)"')
GROUP_TITLE_RE = re.compile(r'group-title="([^"]*)"')


def load_urls(path):
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                name, url = line.split("=", 1)
                entries.append((name.strip(), url.strip()))
            else:
                fallback = os.path.splitext(os.path.basename(line))[0] or f"playlist{len(entries)+1}"
                entries.append((fallback, line))
    return entries


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def filter_playlist(text):
    """
    Returns (body_lines, removed_by_name, removed_by_category,
    tvg_url_or_None). body_lines excludes the #EXTM3U header -- the
    caller writes a single shared header for the combined file. tvg_url
    is whatever url-tvg="..." value was on this source's own header, if
    any.
    """
    lines = text.splitlines()
    kept = []
    removed_by_name = 0
    removed_by_category = 0

    tvg_url = None
    start = 0
    if lines and lines[0].startswith("#EXTM3U"):
        m = URL_TVG_RE.search(lines[0])
        if m:
            tvg_url = m.group(1)
        start = 1

    i = start
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF"):
            # channel name is text after the last comma on the EXTINF line
            name = line.rsplit(",", 1)[-1]
            group_match = GROUP_TITLE_RE.search(line)
            category = group_match.group(1) if group_match else None

            entry_lines = [line]
            i += 1
            # collect any additional tag lines (#EXTVLCOPT, #EXTGRP, etc.) up to the URL
            while i < len(lines) and lines[i].startswith("#"):
                entry_lines.append(lines[i])
                i += 1
            if i < len(lines):
                entry_lines.append(lines[i])  # the stream URL
                i += 1

            if category in CATEGORY_EXCLUDE_LIST:
                removed_by_category += 1
                continue
            if any(term in name for term in GLOBAL_EXCLUDE_LIST):
                removed_by_name += 1
                continue
            kept.extend(entry_lines)
        else:
            kept.append(line)
            i += 1

    return kept, removed_by_name, removed_by_category, tvg_url


def normalize_url(url):
    """
    Strips the query string and fragment so that the same stream served
    with different ad-tracking/session params (a common pattern across
    these providers) compares equal. Scheme/host/path are lowercased for
    the host only, since paths can be case-sensitive.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def dedupe_by_url(lines):
    """
    Removes channel entries whose stream URL -- ignoring query string and
    fragment -- matches one already kept (first occurrence wins). This
    catches the same stream re-listed with different ad-tracking/session
    params, not just byte-identical URLs. Non-channel lines pass through
    unchanged. Returns (deduped_lines, removed_count).
    """
    kept = []
    seen_urls = set()
    removed = 0

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF"):
            entry_lines = [line]
            i += 1
            while i < len(lines) and lines[i].startswith("#"):
                entry_lines.append(lines[i])
                i += 1
            stream_url = None
            if i < len(lines):
                stream_url = lines[i]
                entry_lines.append(stream_url)
                i += 1

            key = normalize_url(stream_url) if stream_url else None
            if key and key in seen_urls:
                removed += 1
                continue
            if key:
                seen_urls.add(key)
            kept.extend(entry_lines)
        else:
            kept.append(line)
            i += 1

    return kept, removed


def main():
    if not os.path.exists(URLS_FILE):
        print(f"Error: {URLS_FILE} not found", file=sys.stderr)
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    entries = load_urls(URLS_FILE)
    if not entries:
        print(f"Error: no URLs found in {URLS_FILE}", file=sys.stderr)
        sys.exit(1)

    total_removed_by_name = 0
    total_removed_by_category = 0
    fetched = 0
    all_body_lines = []
    tvg_urls = []  # de-duplicated, order preserved

    for name, url in entries:
        try:
            raw = fetch(url)
        except Exception as e:
            print(f"[{name}] FAILED to fetch: {e}", file=sys.stderr)
            continue

        body_lines, removed_by_name, removed_by_category, tvg_url = filter_playlist(raw)
        total_removed_by_name += removed_by_name
        total_removed_by_category += removed_by_category
        fetched += 1
        all_body_lines.extend(body_lines)
        if tvg_url and tvg_url not in tvg_urls:
            tvg_urls.append(tvg_url)
        print(f"[{name}] {removed_by_name} removed by name, "
              f"{removed_by_category} removed by category, "
              f"{len(body_lines)} lines kept")

    total_removed = total_removed_by_name + total_removed_by_category
    print(f"Done. {total_removed} channels removed "
          f"({total_removed_by_name} by name, {total_removed_by_category} by category) "
          f"across {fetched}/{len(entries)} source(s).")

    if fetched == 0:
        print("Error: every playlist fetch failed, nothing to write.", file=sys.stderr)
        sys.exit(1)

    all_body_lines, removed_duplicates = dedupe_by_url(all_body_lines)
    print(f"Removed {removed_duplicates} duplicate-stream channels.")

    header = "#EXTM3U"
    if tvg_urls:
        header += f' url-tvg="{",".join(tvg_urls)}"'

    with open(COMBINED_OUTPUT, "w", encoding="utf-8") as f:
        f.write(header + "\n")
        f.write("\n".join(all_body_lines) + "\n")

    print(f"Wrote combined playlist -> {COMBINED_OUTPUT}")


if __name__ == "__main__":
    main()
