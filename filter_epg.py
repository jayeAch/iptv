#!/usr/bin/env python3
"""
Reads tvg-id values out of the already-filtered M3U files in output/,
then streams each XMLTV source listed in epg_urls.txt (plain .xml or
gzipped .xml.gz), keeping only <channel>/<programme> elements whose
id/channel attribute matches a retained tvg-id, and merges everything
into one unified output/epg.xml.

Uses lxml.etree.iterparse + element clearing so large XMLTV files never
get fully loaded into memory.

epg_urls.txt format (one URL per line, blank/# lines ignored):
    https://example.com/epg1.xml.gz
    https://example.com/epg2.xml
"""
import gzip
import io
import os
import re
import sys
import urllib.request
from lxml import etree

M3U_DIR = "output"
EPG_URLS_FILE = "epg_urls.txt"
EPG_OUTPUT = "output/epg.xml"

TVG_ID_RE = re.compile(r'tvg-id="([^"]*)"')


def load_retained_ids(m3u_dir):
    ids = set()
    for fname in os.listdir(m3u_dir):
        if not fname.endswith(".m3u"):
            continue
        with open(os.path.join(m3u_dir, fname), encoding="utf-8") as f:
            for line in f:
                if line.startswith("#EXTINF"):
                    m = TVG_ID_RE.search(line)
                    if m and m.group(1):
                        ids.add(m.group(1))
    return ids


def load_epg_urls(path):
    urls = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def open_source(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=60).read()
    if url.endswith(".gz"):
        return gzip.GzipFile(fileobj=io.BytesIO(data))
    return io.BytesIO(data)


def stream_filter(url, retained_ids, seen_channel_ids, out):
    kept_channels = kept_programmes = 0
    src = open_source(url)
    context = etree.iterparse(src, events=("end",), tag=("channel", "programme"), recover=True)
    for _, elem in context:
        if elem.tag == "channel":
            cid = elem.get("id")
            if cid in retained_ids and cid not in seen_channel_ids:
                out.write(etree.tostring(elem, encoding="unicode"))
                out.write("\n")
                seen_channel_ids.add(cid)
                kept_channels += 1
        else:  # programme
            cid = elem.get("channel")
            if cid in retained_ids:
                out.write(etree.tostring(elem, encoding="unicode"))
                out.write("\n")
                kept_programmes += 1

        elem.clear()
        parent = elem.getparent()
        if parent is not None:
            while elem.getprevious() is not None:
                del parent[0]

    return kept_channels, kept_programmes


def main():
    retained_ids = load_retained_ids(M3U_DIR)
    if not retained_ids:
        print(f"Error: no tvg-id values found in {M3U_DIR}/*.m3u (run filter_m3u.py first)", file=sys.stderr)
        sys.exit(1)
    print(f"{len(retained_ids)} channel IDs retained from M3U filtering")

    urls = load_epg_urls(EPG_URLS_FILE)
    if not urls:
        print(f"Error: no URLs found in {EPG_URLS_FILE}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(EPG_OUTPUT), exist_ok=True)
    seen_channel_ids = set()
    total_channels = total_programmes = 0

    with open(EPG_OUTPUT, "w", encoding="utf-8") as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n')
        for url in urls:
            try:
                c, p = stream_filter(url, retained_ids, seen_channel_ids, out)
                total_channels += c
                total_programmes += p
                print(f"[{url}] {c} channels, {p} programmes kept")
            except Exception as e:
                print(f"[{url}] FAILED: {e}", file=sys.stderr)
        out.write("</tv>\n")

    print(f"Done. {total_channels} channels, {total_programmes} programmes -> {EPG_OUTPUT}")


if __name__ == "__main__":
    main()
