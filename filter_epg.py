#!/usr/bin/env python3
"""
Filters each XMLTV source in epg_urls.txt down to the channels kept in
the M3U of the same name (output/<NAME>.m3u, produced by filter_m3u.py
from the matching NAME in urls.txt), and writes it to output/ as its own
.xml (not merged).

Strictly paired: an epg_urls.txt line "NAME = URL" is processed only if
output/NAME.m3u exists. Bare URLs, and names with no matching M3U, are
skipped without being downloaded.

Usage:
    python filter_epg.py            # every paired entry
    python filter_epg.py TV Plex    # only the named entries

Uses lxml.etree.iterparse + element clearing so large XMLTV files never
get fully loaded into memory.

epg_urls.txt format (one per line, blank/# lines ignored):
    TV   = https://example.com/epg.xml.gz
    Plex = https://example.com/plex.xml
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
WINDOW = timedelta(days=1)  # keep only programmes airing within this span from now

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


def out_of_window(elem, now, window_end):
    """A programme is dropped if it has already ended (stop < now), or
    it starts at/after window_end. Unparseable/missing times keep the
    programme (fail open) so a malformed timestamp can't wipe good data."""
    stop = parse_xmltv_dt(elem.get("stop"))
    if stop is not None and stop < now:
        return True
    start = parse_xmltv_dt(elem.get("start"))
    if start is not None and start >= window_end:
        return True
    return False


def load_retained_ids(paths):
    """
    Returns (ids, names, extinf_count, sample_lines) for the given M3U
    file paths. ids/names are built from tvg-id / tvg-name attributes on
    kept #EXTINF lines. Both are collected because not every provider
    populates tvg-id; the caller decides which set to actually match on.
    """
    ids, names, sample_lines = set(), set(), []
    extinf_count = 0
    for path in paths:
        with open(path, encoding="utf-8") as f:
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


def load_epg_entries(path):
    """Returns a list of (name_or_None, url). 'NAME = URL' pairs the EPG
    with output/<NAME>.m3u; a bare URL is unpaired. A '=' inside the URL
    (query string) is not mistaken for a name separator."""
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            head, sep, tail = line.partition("=")
            if sep and "://" not in head:
                entries.append((head.strip(), tail.strip()))
            else:
                entries.append((None, line))
    return entries


def safe_m3u_name(name):
    return SAFE_NAME_RE.sub("_", name).strip("_") or "playlist"


def _url_path_segments(url):
    from urllib.parse import urlparse
    return [p for p in urlparse(url).path.split("/") if p]


def _strip_xml_ext(name):
    for ext in (".xml.gz", ".xml"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def safe_filename(url):
    segments = _url_path_segments(url)
    base = _strip_xml_ext(segments[-1]) if segments else "epg"
    cleaned = SAFE_NAME_RE.sub("_", base).strip("_")
    return cleaned or "epg"


def safe_filename_with_parent(url):
    """Disambiguated fallback for when safe_filename() collides: prefixes
    the file's parent path segment (e.g. 'Plex/us.xml' -> 'Plex_us'
    instead of the bare 'us' that 'SamsungTVPlus/us.xml' also produces).
    Falls back to safe_filename() if there's no parent segment to use."""
    segments = _url_path_segments(url)
    if len(segments) < 2:
        return safe_filename(url)
    parent = segments[-2]
    base = _strip_xml_ext(segments[-1])
    cleaned = SAFE_NAME_RE.sub("_", f"{parent}_{base}").strip("_")
    return cleaned or safe_filename(url)


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


def stream_filter(url, retained_ids, retained_names, out, now, window_end):
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

    # Pass 2: programmes, against the now-complete matched id set, keeping
    # only what falls in [now, window_end).
    kept_programmes = 0
    dropped_old = 0
    context = etree.iterparse(io.BytesIO(raw), events=("end",), tag="programme", recover=True)
    for _, elem in context:
        cid = elem.get("channel")
        if cid in matched_channel_ids:
            if out_of_window(elem, now, window_end):
                dropped_old += 1
            else:
                out.write(etree.tostring(elem, encoding="unicode"))
                out.write("\n")
                kept_programmes += 1
        _clear(elem)
    del context

    return kept_channels, kept_programmes, dropped_old


def main():
    only = {safe_m3u_name(n) for n in sys.argv[1:]}

    entries = load_epg_entries(EPG_URLS_FILE)
    if not entries:
        print(f"Error: no URLs found in {EPG_URLS_FILE}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    window_end = now + WINDOW
    print(f"Keeping programmes airing between {now.isoformat()} and {window_end.isoformat()} ({WINDOW}).")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    total_channels = total_programmes = total_dropped = 0
    used_filenames = set()
    processed = 0

    for n, (name, url) in enumerate(entries, start=1):
        if not name:
            print(f"[{url}] skipped: no NAME, can't pair with an M3U", file=sys.stderr)
            continue
        key = safe_m3u_name(name)
        if only and key not in only:
            continue
        m3u_path = os.path.join(M3U_DIR, f"{key}.m3u")
        if not os.path.exists(m3u_path):
            print(f"[{name}] skipped: {m3u_path} not found", file=sys.stderr)
            continue

        fname = safe_filename(url)
        if fname in used_filenames:
            candidate = safe_filename_with_parent(url)
            if candidate not in used_filenames and candidate != fname:
                fname = candidate
            else:
                fname = f"{fname}_{n}"  # last-resort fallback, still guaranteed unique
        used_filenames.add(fname)
        out_path = os.path.join(OUTPUT_DIR, f"{fname}.xml")

        ids, names, extinf_count, _ = load_retained_ids([m3u_path])
        if not ids and not names:
            print(f"[{name}] no tvg-id/tvg-name in {extinf_count} #EXTINF lines of {m3u_path}, "
                  f"writing empty EPG -> {out_path}", file=sys.stderr)
            with open(out_path, "w", encoding="utf-8") as out:
                out.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n</tv>\n')
            processed += 1
            continue

        try:
            with open(out_path, "w", encoding="utf-8") as out:
                out.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n')
                c, p, d = stream_filter(url, ids, names, out, now, window_end)
                out.write("</tv>\n")
            total_channels += c
            total_programmes += p
            total_dropped += d
            processed += 1
            print(f"[{name}] {len(ids)} ids, {len(names)} names from {m3u_path}: "
                  f"{c} channels, {p} programmes kept, {d} dropped (too old) -> {out_path}")
        except Exception as e:
            print(f"[{name}] FAILED: {e}", file=sys.stderr)

    print(f"Done. {total_channels} channels, {total_programmes} programmes written, "
          f"{total_dropped} dropped as too old, {processed}/{len(entries)} source(s) processed.")


if __name__ == "__main__":
    main()
