#!/usr/bin/env python3
"""
Deletes playlists/*.m3u and playlists/*.xml whose NAME is no longer listed in
m3u_urls.txt (e.g. a source you commented out or removed). Files for sources
that are still listed are never touched, so a source that failed this run
keeps its last good copy. combined.* is always kept; other file types
(e.g. Placeholder) are ignored.
"""
import os
import sys

from filter_m3u import COMBINED_NAME, URLS_FILE, group_entries, load_urls

PLAYLISTS_DIR = "playlists"


def main():
    entries = load_urls(URLS_FILE)
    if not entries:
        print(f"Error: no sources in {URLS_FILE}, refusing to prune.", file=sys.stderr)
        sys.exit(1)

    keep = {fname.casefold() for _, fname, _ in group_entries(entries)}
    keep.add(COMBINED_NAME.casefold())

    if not os.path.isdir(PLAYLISTS_DIR):
        return

    for f in sorted(os.listdir(PLAYLISTS_DIR)):
        stem, ext = os.path.splitext(f)
        if ext.lower() in (".m3u", ".xml") and stem.casefold() not in keep:
            os.remove(os.path.join(PLAYLISTS_DIR, f))
            print(f"pruned stale playlists/{f}")


if __name__ == "__main__":
    main()
