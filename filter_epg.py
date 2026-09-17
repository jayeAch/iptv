#!/usr/bin/env python3
"""
Reads tvg-id values out of the already-filtered M3U files in output/,
then streams each XMLTV source listed in epg_urls.txt (plain .xml or
gzipped .xml.gz), keeping only <channel>/<programme> elements whose
id/channel attribute matches a retained tvg-id, and writes each source
out as its own file in output/ (one .xml per line in epg_urls.txt, not
merged together).

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
from datetime import datetime, timedelta, timezone
from lxml import etree

M3U_DIR = "output"
EPG_URLS_FILE = "epg_urls.txt"
OUTPUT_DIR = "output"
MAX_AGE = timedelta(days=1)  # drop programmes that ended more than this long ago

TVG_ID_RE = re.compile(r'tvg-id=["\']([^"\']*)["\']', re.IGNORECASE)
TVG_NAME_RE = re.compile(r'tvg-name=["\']([^"\']*)["\']', re.IGNORECASE)
SAFE_NAME_RE = re.compile(r'[^A-Za-z0-9_-]+')
XMLTV_DT_RE = re.compile(r'^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\s*([+-]\d{4})?$')


def parse_xmltv_dt(value):
    """Parses an XMLTV datetime ('20260917013000 +0000') into an aware
    datetime. Returns None if value is missing/unparseable, so callers
    can fail open (keep the programme) rather than drop good data."""
    if not value:
        return None
    m = XMLTV_DT_RE.match(value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, off = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    if off:
        sign = 1 if off[0] == "+" else -1
        off_h, off_m = int(off[1:3]), int(off[3:5])
        dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=off_h, minutes=off_m)))
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_too_old(elem, cutoff):
    """A programme is dropped only if it has a parseable stop/start time
    AND that time is before cutoff. Unparseable or missing times keep
    the programme (fail open)."""
    dt = parse_xmltv_dt(elem.get("stop")) or parse_xmltv_dt(elem.get("start"))
    return dt is not None and dt < cutoff


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


def safe_filename(url):
    base = url.rsplit("/", 1)[-1]
    for ext in (".xml.gz", ".xml"):
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    cleaned = SAFE_NAME_RE.sub("_", base).strip("_")
    return cleaned or "epg"


def fetch_source_bytes(url):
    """Downloads and fully decompresses the source once, returning raw XML bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=60).read()
    if url.endswith(".gz"):
        return gzip.GzipFile(fileobj=io.BytesIO(data)).read()
    return data


def _clear(elem):
    elem.clear()
    parent = elem.getparent()
    if parent is not None:
        while elem.getprevious() is not None:
            del parent[0]


def stream_filter(url, retained_ids, retained_names, out, cutoff):
    """
    A <channel> is kept if its id matches retained_ids, or one of its
    <display-name> values matches retained_names (covers providers that
    leave tvg-id blank in the M3U). Once a channel id is confirmed a
    match, any <programme channel="..."> referencing that id is kept too
    -- this is what lets name-only matching still filter programmes,
    since programme elements only ever carry the channel id, never a name.

    Two passes over the same buffered bytes, so this does not depend on
    <channel> elements appearing before the <programme> elements that
    reference them -- some sources interleave or reverse the order.
    Pass 1 resolves the complete set of matched channel ids and writes
    the (deduped) <channel> elements. Pass 2 filters <programme>
    elements against that now-complete set. Each pass still streams via
    iterparse + clearing, so peak memory stays bounded by one element at
    a time, not the whole tree -- only the raw source bytes are held
    twice (once as downloaded, once per gzip.read() above).
    """
    raw = fetch_source_bytes(url)

    # Pass 1: channels only.
    matched_channel_ids = set(retained_ids)
    seen_channel_ids = set()
    kept_channels = 0
    context = etree.iterparse(io.BytesIO(raw), events=("end",), tag="channel", recover=True)
    for _, elem in context:
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
        _clear(elem)
    del context

    # Pass 2: programmes, against the now-complete matched id set, skipping
    # anything that ended before cutoff.
    kept_programmes = 0
    dropped_old = 0
    context = etree.iterparse(io.BytesIO(raw), events=("end",), tag="programme", recover=True)
    for _, elem in context:
        cid = elem.get("channel")
        if cid in matched_channel_ids:
            if is_too_old(elem, cutoff):
                dropped_old += 1
            else:
                out.write(etree.tostring(elem, encoding="unicode"))
                out.write("\n")
                kept_programmes += 1
        _clear(elem)
    del context

    return kept_channels, kept_programmes, dropped_old


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

    cutoff = datetime.now(timezone.utc) - MAX_AGE
    print(f"Dropping programmes that ended before {cutoff.isoformat()} (older than {MAX_AGE}).")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    total_channels = total_programmes = total_dropped = 0
    used_filenames = set()

    for n, url in enumerate(urls, start=1):
        fname = safe_filename(url)
        if fname in used_filenames:
            fname = f"{fname}_{n}"
        used_filenames.add(fname)
        out_path = os.path.join(OUTPUT_DIR, f"{fname}.xml")

        try:
            with open(out_path, "w", encoding="utf-8") as out:
                out.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n')
                c, p, d = stream_filter(url, retained_ids, retained_names, out, cutoff)
                out.write("</tv>\n")
            total_channels += c
            total_programmes += p
            total_dropped += d
            print(f"[{url}] {c} channels, {p} programmes kept, {d} dropped (too old) -> {out_path}")
        except Exception as e:
            print(f"[{url}] FAILED: {e}", file=sys.stderr)

    print(f"Done. {total_channels} channels, {total_programmes} programmes written, "
          f"{total_dropped} dropped as too old, across {len(urls)} source(s).")


if __name__ == "__main__":
    main()
