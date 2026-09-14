#!/usr/bin/env python3
"""
Downloads each M3U URL listed in urls.txt, strips any channel whose
#EXTINF line matches a word/substring in GLOBAL_EXCLUDE_LIST, and writes
the cleaned playlist to output/<name>.m3u.

urls.txt format (one entry per line, blank lines and lines starting with
# are ignored):
    MyProvider = https://example.com/playlist1.m3u8
    https://example.com/playlist2.m3u8

If no "name = " prefix is given, the URL's filename (or its position)
is used to name the output file.
"""
import os
import re
import sys
import urllib.request
from exclude_list import GLOBAL_EXCLUDE_LIST

URLS_FILE = "urls.txt"
OUTPUT_DIR = "output"


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
    lines = text.splitlines()
    kept = []
    i = 0
    removed = 0
    header = lines[0] if lines and lines[0].startswith("#EXTM3U") else None
    start = 1 if header else 0
    if header:
        kept.append(header)

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

    return "\n".join(kept) + "\n", removed


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
    for name, url in entries:
        safe_name = re.sub(r"[^\w.-]+", "_", name)
        try:
            raw = fetch(url)
        except Exception as e:
            print(f"[{name}] FAILED to fetch: {e}", file=sys.stderr)
            continue

        cleaned, removed = filter_playlist(raw)
        total_removed += removed
        out_path = os.path.join(OUTPUT_DIR, f"{safe_name}.m3u")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(cleaned)
        print(f"[{name}] {removed} channels removed -> {out_path}")

    print(f"Done. {total_removed} channels removed across {len(entries)} playlist(s).")


if __name__ == "__main__":
    main()
