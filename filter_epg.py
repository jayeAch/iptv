#!/usr/bin/env python3
"""
Filters each XMLTV source in epg_urls.txt down to the channels kept in
the M3U of the same name (output/<NAME>.m3u, produced by filter_m3u.py
from the matching NAME in m3u_urls.txt), and writes it to output/<NAME>.xml (not merged), so
every EPG is named after its streaming service.

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

Channel matching:
  1. A <channel id> equal to a tvg-id from the M3U is kept.
  2. For M3U entries with no tvg-id, or whose tvg-id doesn't exist in the
     EPG, the tvg-name / title (case- and whitespace-insensitive) is
     matched against <display-name>. Each name is claimed by only one
     EPG channel, so duplicate display names can't pull in extra channels.

Time filtering keeps programmes airing in [now, now + WINDOW).
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
UNKNOWN_STOP_MAX = timedelta(hours=4)  # programme with no stop: dropped if it started longer ago than this

ATTR_RES = {
    key: re.compile(r'(?<![\w-])' + key + r'=(?:"([^"]*)"|\'([^\']*)\')', re.IGNORECASE)
    for key in ("tvg-id", "tvg-name")
}
SAFE_NAME_RE = re.compile(r'[^A-Za-z0-9_-]+')
XMLTV_DT_RE = re.compile(
    r'^(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?\s*(?:(Z)|([+-])(\d{2}):?(\d{2}))?$',
    re.IGNORECASE,
)


def norm(s):
    """Case-insensitive, whitespace-collapsed form used for name matching."""
    return " ".join((s or "").split()).casefold()


def parse_xmltv_dt(value):
    """Parses an XMLTV datetime ('20260917013000 +0000', '20260917013000+00:00',
    '202609170130', '20260917013000 Z') into an aware datetime. Returns None
    if missing/unparseable/out of range, so callers can fail open (keep the
    programme) rather than drop good data or crash."""
    if not value:
        return None
    m = XMLTV_DT_RE.match(value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, z, sign, oh, om = m.groups()
    try:
        tz = timezone.utc
        if sign:
            delta = timedelta(hours=int(oh), minutes=int(om))
            tz = timezone(delta if sign == "+" else -delta)
        return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0), tzinfo=tz)
    except ValueError:
        return None


def window_status(elem, now, window_end):
    """Returns 'past' if the programme has ended, 'future' if it starts at or
    after window_end, else None (keep). A missing stop falls back to start
    (dropped as 'past' if it began more than UNKNOWN_STOP_MAX ago). Missing or
    unparseable times keep the programme (fail open)."""
    stop = parse_xmltv_dt(elem.get("stop"))
    start = parse_xmltv_dt(elem.get("start"))
    if stop is not None:
        if stop < now:
            return "past"
    elif start is not None and start < now - UNKNOWN_STOP_MAX:
        return "past"
    if start is not None and start >= window_end:
        return "future"
    return None


def get_attr(line, key):
    m = ATTR_RES[key].search(line)
    if not m:
        return ""
    return (m.group(1) if m.group(1) is not None else m.group(2) or "").strip()


def load_m3u_entries(paths):
    """
    Returns (entries, extinf_count) for the given M3U file paths.
    entries is a list of (tvg_id, names) per #EXTINF line, where names is a
    set of normalized tvg-name / trailing title values. Quoted attributes
    may contain the other quote character (e.g. "Bob's Burgers").
    """
    entries = []
    extinf_count = 0
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.startswith("#EXTINF"):
                    continue
                extinf_count += 1
                tvg_id = get_attr(line, "tvg-id")
                names = set()
                tvg_name = norm(get_attr(line, "tvg-name"))
                if tvg_name:
                    names.add(tvg_name)
                title = norm(line.rstrip("\r\n").rsplit(",", 1)[-1]) if "," in line else ""
                if title:
                    names.add(title)
                if tvg_id or names:
                    entries.append((tvg_id, names))
    return entries, extinf_count


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


def fetch_source_bytes(url):
    """Downloads the source once and returns raw XML bytes, gunzipping when the
    payload starts with the gzip magic bytes (works for URLs with query strings)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
    return data


def _clear(elem):
    elem.clear()
    parent = elem.getparent()
    if parent is not None:
        while elem.getprevious() is not None:
            del parent[0]


def stream_filter(url, entries, out, now, window_end):
    """
    Pass 1 streams <channel> elements (and clears <programme> elements so they
    never accumulate in memory), collecting each channel's id, normalized
    display names and serialized XML. Matching is then resolved against the
    complete channel list, so source element order doesn't matter.
    Pass 2 streams <programme> elements, keeping those whose channel matched
    and which fall inside [now, window_end).

    Returns (kept_channels, kept_programmes, dropped_past, dropped_future).
    """
    raw = fetch_source_bytes(url)

    # Pass 1: channels.
    epg_channels = []  # (cid, normalized display names, xml)
    context = etree.iterparse(io.BytesIO(raw), events=("end",), tag=("channel", "programme"), recover=True)
    for _, elem in context:
        if elem.tag == "channel":
            cid = (elem.get("id") or "").strip()
            if cid:
                dnames = {norm(dn.text) for dn in elem.findall("display-name")}
                dnames.discard("")
                xml = etree.tostring(elem, encoding="unicode", with_tail=False)
                epg_channels.append((cid, dnames, xml))
        _clear(elem)
    del context

    epg_ids = {cid for cid, _, _ in epg_channels}
    m3u_ids = {i for i, _ in entries if i}
    # Name fallback only for M3U entries with no tvg-id or whose tvg-id isn't in this EPG.
    fallback_names = set()
    for tvg_id, names in entries:
        if not tvg_id or tvg_id not in epg_ids:
            fallback_names |= names

    matched_ids = set()
    name_owner = {}
    kept_channels = 0
    for cid, dnames, xml in epg_channels:
        if cid in matched_ids:
            continue  # duplicate <channel> id
        is_match = cid in m3u_ids
        if not is_match:
            claimable = [h for h in (dnames & fallback_names) if name_owner.get(h, cid) == cid]
            if claimable:
                is_match = True
                for h in claimable:
                    name_owner[h] = cid
        if is_match:
            matched_ids.add(cid)
            out.write(xml)
            out.write("\n")
            kept_channels += 1

    # Pass 2: programmes.
    kept_programmes = dropped_past = dropped_future = 0
    context = etree.iterparse(io.BytesIO(raw), events=("end",), tag="programme", recover=True)
    for _, elem in context:
        if (elem.get("channel") or "").strip() in matched_ids:
            status = window_status(elem, now, window_end)
            if status == "past":
                dropped_past += 1
            elif status == "future":
                dropped_future += 1
            else:
                out.write(etree.tostring(elem, encoding="unicode", with_tail=False))
                out.write("\n")
                kept_programmes += 1
        _clear(elem)
    del context

    return kept_channels, kept_programmes, dropped_past, dropped_future


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
    total_channels = total_programmes = total_past = total_future = 0
    used_filenames = set()
    processed = 0

    for name, url in entries:
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

        if key in used_filenames:
            print(f"[{name}] skipped: duplicate NAME '{key}' in {EPG_URLS_FILE}", file=sys.stderr)
            continue
        used_filenames.add(key)
        out_path = os.path.join(OUTPUT_DIR, f"{key}.xml")

        m3u_entries, extinf_count = load_m3u_entries([m3u_path])
        if not m3u_entries:
            print(f"[{name}] no tvg-id/tvg-name/title in {extinf_count} #EXTINF lines of {m3u_path}, "
                  f"writing empty EPG -> {out_path}", file=sys.stderr)
            with open(out_path, "w", encoding="utf-8") as out:
                out.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n</tv>\n')
            processed += 1
            continue

        try:
            with open(out_path, "w", encoding="utf-8") as out:
                out.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n')
                c, p, dp, df = stream_filter(url, m3u_entries, out, now, window_end)
                out.write("</tv>\n")
            total_channels += c
            total_programmes += p
            total_past += dp
            total_future += df
            processed += 1
            print(f"[{name}] {len(m3u_entries)} M3U entries from {m3u_path}: "
                  f"{c} channels, {p} programmes kept, {dp} dropped (ended), "
                  f"{df} dropped (beyond window) -> {out_path}")
        except Exception as e:
            print(f"[{name}] FAILED: {e}", file=sys.stderr)

    print(f"Done. {total_channels} channels, {total_programmes} programmes written, "
          f"{total_past} dropped as ended, {total_future} dropped as beyond window, "
          f"{processed}/{len(entries)} source(s) processed.")


if __name__ == "__main__":
    main()
