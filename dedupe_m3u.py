#!/usr/bin/env python3
"""
Removes duplicate channels from the M3U lists, using EPG data (what is airing
right now) as the evidence. Works on the per-source output/NAME.m3u +
output/NAME.xml written by filter_m3u.py / filter_epg.py, and drops the removed
channels' guide data from the .xml files so the EPG still matches the playlists.

Two channels are duplicates when the programme airing right now has the same
title (small wording differences between sources are tolerated) and the same
start/stop (within TOLERANCE) on both, and then either at
least MIN_EXTRA_MATCHES other programmes also match, or nothing contradicts
them. So a channel with a shorter guide (say it lists only the current show)
still counts as a duplicate of one with the full guide, unless a later
programme on one overlaps a different programme on the other. Placeholder titles
(filler, "to be announced", or the channel's own name) never count.

Which copy is kept: channels tied to Samsung (source NAME, stream URL or
#EXTINF line containing "samsung" / "stvp-") are always dropped first, so a
Samsung channel is only removed when a non-Samsung copy exists. Among the
rest, a short guide loses to a full one, then the earliest source in
m3u_urls.txt wins, then the shorter channel name ("TBS" over "TBS West"), then
the channel with more programme data. Channels with no EPG data are never
touched.

Run after filter_epg.py. Rewrites output/NAME.m3u, output/NAME.xml,
output/combined.m3u and output/combined.xml. Idempotent.

Usage:
    python dedupe_m3u.py            # apply
    python dedupe_m3u.py --dry-run  # report only, write nothing
    python dedupe_m3u.py --now 2026-10-25T00:22:12Z   # fixed reference time
"""
import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from lxml import etree

import filter_m3u as fm
from filter_epg import _clear, get_attr, merge_epgs, norm, parse_xmltv_dt

OUTPUT_DIR = "output"
TOLERANCE = timedelta(seconds=120)   # max start/stop difference for "same programme"
MIN_EXTRA_MATCHES = 2                # other matching programmes that confirm a duplicate
TITLE_RATIO = 0.85                  # title similarity counted as the same title
SHORT_GUIDE = 3                      # guides with fewer programmes are kept last
SAMSUNG_RE = re.compile(r"samsung|(?<![a-z0-9])stvp-", re.IGNORECASE)
GENERIC_TITLES = {
    "", "filler", "to be announced", "tba", "tbd", "programming",
    "no information available", "no programming", "off air", "sign off",
}


def title_key(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").casefold()).strip()


def load_source(name):
    """Returns (items, units) for output/NAME.m3u.
    units: {cid: {"entries": [item indexes], "names": {...}}}; entries that
    can't be tied to an EPG channel are left out and never dropped."""
    m3u_path = os.path.join(OUTPUT_DIR, f"{name}.m3u")
    xml_path = os.path.join(OUTPUT_DIR, f"{name}.xml")
    with open(m3u_path, encoding="utf-8") as f:
        items, _, _ = fm.parse_playlist(f.read())

    ids, by_name = {}, {}
    for _, e in etree.iterparse(xml_path, events=("end",), tag="channel", recover=True):
        cid = (e.get("id") or "").strip()
        if cid:
            dnames = {norm(d.text) for d in e.findall("display-name")} - {""}
            ids[cid] = dnames
            for d in dnames:
                by_name.setdefault(d, cid)
        _clear(e)

    units = {}
    for idx, it in enumerate(items):
        if it[0] != "entry":
            continue
        extinf = it[1]
        tvg_id = get_attr(extinf, "tvg-id")
        cid = tvg_id if tvg_id in ids else None
        if cid is None:
            for n in (norm(get_attr(extinf, "tvg-name")), norm(fm.entry_name(extinf))):
                if n and n in by_name:
                    cid = by_name[n]
                    break
        if cid is None:
            continue
        u = units.setdefault(cid, {"entries": [], "names": set(ids[cid])})
        u["entries"].append(idx)
        u["names"].add(norm(fm.entry_name(extinf)))
    return items, units


def load_programmes(name, wanted):
    """{cid: [(start, stop, title_key)]} for the wanted channel ids."""
    progs = defaultdict(list)
    xml_path = os.path.join(OUTPUT_DIR, f"{name}.xml")
    for _, e in etree.iterparse(xml_path, events=("end",), tag="programme", recover=True):
        cid = (e.get("channel") or "").strip()
        if cid in wanted:
            st, sp = parse_xmltv_dt(e.get("start")), parse_xmltv_dt(e.get("stop"))
            if st and sp:
                progs[cid].append((st, sp, title_key(e.findtext("title"))))
        _clear(e)
    for v in progs.values():
        v.sort()
    return progs


def titles_match(a, b):
    """Same title, tolerating the small wording differences between sources:
    spacing ("cell mate"/"cellmate"), a subtitle ("the divergent series" vs
    "the divergent series insurgent"), or a typo."""
    if a == b:
        return True
    sa, sb = a.replace(" ", ""), b.replace(" ", "")
    short, long_ = sorted((sa, sb), key=len)
    if len(short) >= 6 and long_.startswith(short):
        return True
    return SequenceMatcher(None, sa, sb).ratio() >= TITLE_RATIO


def same(a, b):
    return (abs(a[0] - b[0]) <= TOLERANCE and abs(a[1] - b[1]) <= TOLERANCE
            and titles_match(a[2], b[2]))


def usable(prog, names):
    return prog[2] not in GENERIC_TITLES and prog[2] not in names


def current(progs, names, now):
    for p in progs:
        if p[0] <= now < p[1] and usable(p, names):
            return p
    return None


def overlaps(p, q):
    return p[0] < q[1] - TOLERANCE and q[0] < p[1] - TOLERANCE


def compare(u, v, now):
    """Returns the number of matching programmes if u and v are the same feed,
    otherwise 0. They must air the same thing right now. After that, either at
    least MIN_EXTRA_MATCHES other programmes match, or nothing contradicts them:
    a channel whose guide ends early (or lists only the current programme) is
    still a duplicate as long as no programme on one overlaps a different
    programme on the other."""
    cu, cv = current(u["progs"], u["names"], now), current(v["progs"], v["names"], now)
    if not cu or not cv or not same(cu, cv):
        return 0
    names = u["names"] | v["names"]
    up = [p for p in u["progs"] if usable(p, names)]
    vp = [q for q in v["progs"] if usable(q, names)]
    extra = sum(1 for p in up if p is not cu and any(same(p, q) for q in vp))
    if extra >= MIN_EXTRA_MATCHES:
        return 1 + extra
    conflict = any(p is not cu and q is not cv and overlaps(p, q) and not same(p, q)
                   for p in up for q in vp)
    return 1 + extra if not conflict else 0


def rewrite_xml(path, drop_ids):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n')
        for _, e in etree.iterparse(path, events=("end",), tag=("channel", "programme"), recover=True):
            cid = e.get("id") if e.tag == "channel" else e.get("channel")
            if (cid or "").strip() not in drop_ids:
                out.write(etree.tostring(e, encoding="unicode", with_tail=False))
                out.write("\n")
            _clear(e)
        out.write("</tv>\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help="ISO 8601 reference time (default: current UTC time)")
    args = ap.parse_args()
    now = (datetime.fromisoformat(args.now.replace("Z", "+00:00"))
           if args.now else datetime.now(timezone.utc))

    groups = fm.group_entries(fm.load_urls(fm.URLS_FILE))
    sources = [f for _, f, _ in groups
               if os.path.exists(os.path.join(OUTPUT_DIR, f"{f}.m3u"))
               and os.path.exists(os.path.join(OUTPUT_DIR, f"{f}.xml"))]
    if not sources:
        print("No paired output/*.m3u + *.xml found, nothing to do.", file=sys.stderr)
        return

    data = {}      # source -> items
    allunits = []  # [{"src", "cid", "order", "samsung", "names", "progs", ...}]
    for order, name in enumerate(sources):
        items, units = load_source(name)
        progs = load_programmes(name, set(units))
        data[name] = items
        for cid, u in units.items():
            if not progs.get(cid):
                continue
            blob = " ".join([name] + [items[i][1] + " " + items[i][3] for i in u["entries"]])
            allunits.append({
                "src": name, "cid": cid, "order": order, "entries": u["entries"],
                "names": u["names"], "progs": progs[cid],
                "samsung": bool(SAMSUNG_RE.search(blob)),
                "label": fm.entry_name(items[u["entries"][0]][1]),
            })

    # Non-Samsung first, then source order, then more programme data.
    # Samsung last, then short guides, then source order, shorter name, more data.
    allunits.sort(key=lambda u: (u["samsung"], len(u["progs"]) < SHORT_GUIDE, u["order"],
                                 len(u["label"]), -len(u["progs"]), u["label"].casefold()))

    kept, dropped = [], []
    for u in allunits:
        hit = None
        for k in kept:
            n = compare(u, k, now)
            if n:
                hit = (k, n)
                break
        if hit:
            dropped.append((u, *hit))
        else:
            kept.append(u)

    for u, k, n in dropped:
        cur = current(u["progs"], u["names"], now)
        print(f"DROP {u['src']}: {u['label']}  ->  keep {k['src']}: {k['label']}  "
              f"({n} programmes match; now: {cur[2] if cur else '?'})")
    print(f"{len(dropped)} duplicate channel(s) of {len(allunits)} compared.")

    if args.dry_run or not dropped:
        return

    drop_idx = defaultdict(set)   # source -> item indexes
    drop_cids = defaultdict(set)  # source -> epg channel ids
    for u, _, _ in dropped:
        drop_idx[u["src"]].update(u["entries"])
        drop_cids[u["src"]].add(u["cid"])

    combined_entries = []
    for name in sources:
        items = [it for i, it in enumerate(data[name]) if i not in drop_idx[name]]
        combined_entries.extend(it for it in items if it[0] == "entry")
        if drop_idx[name]:
            with open(os.path.join(OUTPUT_DIR, f"{name}.m3u"), "w", encoding="utf-8") as f:
                f.write(fm.render(items))
            rewrite_xml(os.path.join(OUTPUT_DIR, f"{name}.xml"), drop_cids[name])

    combined_items, _ = fm.dedupe_items(combined_entries, set())
    with open(os.path.join(OUTPUT_DIR, f"{fm.COMBINED_NAME}.m3u"), "w", encoding="utf-8") as f:
        f.write(fm.render(combined_items))
    c, p = merge_epgs([(n, os.path.join(OUTPUT_DIR, f"{n}.xml")) for n in sources],
                      os.path.join(OUTPUT_DIR, f"{fm.COMBINED_NAME}.xml"))
    print(f"[{fm.COMBINED_NAME}] {len(combined_items)} channels; EPG {c} channels, {p} programmes")


if __name__ == "__main__":
    main()
