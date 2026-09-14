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

TVG_ID_RE = re.compile(r'tvg-id=["\']([^"\']*)["\']', re.IGNORECASE)
TVG_NAME_RE = re.compile(r'tvg-name=["\']([^"\']*)["\']', re.IGNORECASE)


def load_retained_ids(m3u_dir):
    """
    Returns (ids, names, extinf_count, sample_lines).
    ids/names are built from tvg-id / tvg-name attributes on kept #EXTINF
    lines. Both are collected because not every provider populates tvg-id;
    the caller decides which set to actually match against.
    """
    ids, names, sample_lines = set(), set(), []
    extinf_count = 0
    for fname in sorted(os.listdir(m3u_dir)):
        if not fname.endswith(".m3u"):
            continue
        with open(os.path.join(m3u_dir, fname), encoding="utf-8") as f:
            for line in f:
                if line.startswith("#EXTINF"):
                    extinf_count += 1
                    if len(sample_lines) < 5:
                        sample_lines.append(line.strip())
                    m_id = TVG_ID_RE.search(line)
                    if m_id and m_id.group(1):
                        ids.add(m_id.group(1))
                    m_name = TVG_NAME_RE.search(line)
                    if m_name and m_name.group(1):
                        names.add(m_name.group(1))
    return ids, names, extinf_count, sample_lines


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


def stream_filter(url, retained_ids, retained_names, seen_channel_ids, out):
    """
    A <channel> is kept if its id matches retained_ids, or one of its
    <display-name> values matches retained_names (covers providers that
    leave tvg-id blank in the M3U). Once a channel id is confirmed a
    match, any <programme channel="..."> referencing that id is kept too
    -- this is what lets name-only matching still filter programmes,
    since programme elements only ever carry the channel id, never a name.
    Assumes <channel> elements appear before the <programme> elements that
    reference them, which holds for standard XMLTV output.
    """
    kept_channels = kept_programmes = 0
    matched_channel_ids = set(retained_ids)
    src = open_source(url)
    context = etree.iterparse(src, events=("end",), tag=("channel", "programme"), recover=True)
    for _, elem in context:
        if elem.tag == "channel":
            cid = elem.get("id")
            display_names = {(dn.text or "").strip() for dn in elem.findall("display-name")}
            is_match = cid in retained_ids or bool(display_names & retained_names)
            if is_match and cid:
                matched_channel_ids.add(cid)
                if cid not in seen_channel_ids:
                    out.write(etree.tostring(elem, encoding="unicode"))
                    out.write("\n")
                    seen_channel_ids.add(cid)
                    kept_channels += 1
        else:  # programme
            cid = elem.get("channel")
            if cid in matched_channel_ids:
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
    retained_ids, retained_names, extinf_count, sample_lines = load_retained_ids(M3U_DIR)

    if not retained_ids and not retained_names:
        print(f"Error: found {extinf_count} #EXTINF lines in {M3U_DIR}/*.m3u but none "
              f"carry a tvg-id or tvg-name attribute -- nothing to match against.", file=sys.stderr)
        if sample_lines:
            print("Sample #EXTINF lines seen:", file=sys.stderr)
            for line in sample_lines:
                print(f"  {line}", file=sys.stderr)
        sys.exit(1)

    print(f"{len(retained_ids)} channel IDs and {len(retained_names)} channel names "
          f"retained from M3U filtering ({extinf_count} #EXTINF lines scanned)")
    if not retained_ids:
        print("Note: no tvg-id values found, matching by tvg-name/display-name instead.")

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
                c, p = stream_filter(url, retained_ids, retained_names, seen_channel_ids, out)
                total_channels += c
                total_programmes += p
                print(f"[{url}] {c} channels, {p} programmes kept")
            except Exception as e:
                print(f"[{url}] FAILED: {e}", file=sys.stderr)
        out.write("</tv>\n")

    print(f"Done. {total_channels} channels, {total_programmes} programmes -> {EPG_OUTPUT}")


if __name__ == "__main__":
    main()
