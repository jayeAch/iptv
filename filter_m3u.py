#!/usr/bin/env python3
"""
Downloads each M3U URL listed in urls.txt, strips any channel whose
#EXTINF line matches a word/substring in GLOBAL_EXCLUDE_LIST, and merges
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
from exclude_list import GLOBAL_EXCLUDE_LIST

URLS_FILE = "urls.txt"
OUTPUT_DIR = "output"
COMBINED_OUTPUT = os.path.join(OUTPUT_DIR, "combined.m3u")

URL_TVG_RE = re.compile(r'url-tvg="([^"]*)"')


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
    Returns (body_lines, removed_count, tvg_url_or_None). body_lines
    excludes the #EXTM3U header -- the caller writes a single shared
    header for the combined file. tvg_url is whatever url-tvg="..."
    value was on this source's own header, if any.
    """
    lines = text.splitlines()
    kept = []
    removed = 0

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
            entry_lines = [line]
            i += 1
            # collect any additional tag lines (#EXTVLCOPT, #EXTGRP, etc.) up to the URL
            while i < len(lines) and lines[i].startswith("#"):
                entry_lines.append(lines[i])
                i += 1
            if i < len(lines):
                entry_lines.append(lines[i])  # the stream URL
                i += 1

            if any(term in name for term in GLOBAL_EXCLUDE_LIST):
                removed += 1
                continue
            kept.extend(entry_lines)
        else:
            kept.append(line)
            i += 1

    return kept, removed, tvg_url


def main():
    if not os.path.exists(URLS_FILE):
        print(f"Error: {URLS_FILE} not found", file=sys.stderr)
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    entries = load_urls(URLS_FILE)
    if not entries:
        print(f"Error: no URLs found in {URLS_FILE}", file=sys.stderr)
        sys.exit(1)

    total_removed = 0
    fetched = 0
    all_body_lines = []
    tvg_urls = []  # de-duplicated, order preserved

    for name, url in entries:
        try:
            raw = fetch(url)
        except Exception as e:
            print(f"[{name}] FAILED to fetch: {e}", file=sys.stderr)
            continue

        body_lines, removed, tvg_url = filter_playlist(raw)
        total_removed += removed
        fetched += 1
        all_body_lines.extend(body_lines)
        if tvg_url and tvg_url not in tvg_urls:
            tvg_urls.append(tvg_url)
        print(f"[{name}] {removed} channels removed, {len(body_lines)} lines kept")

    print(f"Done. {total_removed} channels removed across {fetched}/{len(entries)} source(s).")

    if fetched == 0:
        print("Error: every playlist fetch failed, nothing to write.", file=sys.stderr)
        sys.exit(1)

    header = "#EXTM3U"
    if tvg_urls:
        header += f' url-tvg="{",".join(tvg_urls)}"'

    with open(COMBINED_OUTPUT, "w", encoding="utf-8") as f:
        f.write(header + "\n")
        f.write("\n".join(all_body_lines) + "\n")

    print(f"Wrote combined playlist -> {COMBINED_OUTPUT}")


if __name__ == "__main__":
    main()
