#!/usr/bin/env python3
"""
XUMO's guide lists the episode name as <title> (e.g. "The Hat; Grieving
Process") with no series name or sub-title. This rewrites those programmes to
the layout the other sources use -- <title>Creepshow</title>,
<sub-title>The Hat; Grieving Process</sub-title>, plus the episode-num -- by
finding the same episode name as a <sub-title> in another source's guide.
XUMO's own times and description are kept. Programmes with no match, or whose
episode name maps to more than one show, are left untouched.

Run after filter_epg.py and before dedupe_m3u.py. Rewrites output/XUMO.xml and
output/combined.xml. Idempotent.

Usage:
    python align_xumo_epg.py            # apply
    python align_xumo_epg.py --dry-run  # report only, write nothing
"""
import argparse
import os
import re
import sys
from collections import defaultdict

from lxml import etree

import filter_m3u as fm
from filter_epg import _clear, merge_epgs

OUTPUT_DIR = "output"
TARGETS = {"XUMO"}      # sources to rewrite
MIN_LEN = 6             # ignore very short episode names ("Pilot", "Home")


def key(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").casefold()).strip()


def build_index(sources):
    """{normalized sub-title: {(show title, episode-num or None)}} from other sources."""
    idx = defaultdict(set)
    for name in sources:
        path = os.path.join(OUTPUT_DIR, f"{name}.xml")
        for _, e in etree.iterparse(path, events=("end",), tag="programme", recover=True):
            title, sub = (e.findtext("title") or "").strip(), (e.findtext("sub-title") or "").strip()
            k = key(sub)
            if title and k and k != key(title) and len(k) >= MIN_LEN:
                ep = next((x.text for x in e.findall("episode-num")
                           if x.get("system") in ("onscreen", "xmltv_ns") and x.text), None)
                idx[k].add((title, ep))
            _clear(e)
    return idx


def align(name, idx, dry):
    path = os.path.join(OUTPUT_DIR, f"{name}.xml")
    tmp, changed, total = path + ".tmp", 0, 0
    out = None if dry else open(tmp, "w", encoding="utf-8")
    try:
        if out:
            out.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n')
        for _, e in etree.iterparse(path, events=("end",), tag=("channel", "programme"), recover=True):
            if e.tag == "programme":
                total += 1
                t = e.find("title")
                if t is not None and e.find("sub-title") is None:
                    hits = idx.get(key(t.text))
                    if hits and len({h[0] for h in hits}) == 1:
                        show = next(iter(hits))[0]
                        ep = next((h[1] for h in sorted(hits, key=lambda h: h[1] or "") if h[1]), None)
                        episode = t.text
                        t.text = show
                        sub = etree.Element("sub-title", lang="en")
                        sub.text = episode
                        t.addnext(sub)
                        if ep:
                            sub.addnext(etree.Element("episode-num", system="onscreen"))
                            sub.getnext().text = ep
                        changed += 1
            if out:
                out.write(etree.tostring(e, encoding="unicode", with_tail=False) + "\n")
            _clear(e)
        if out:
            out.write("</tv>\n")
    finally:
        if out:
            out.close()
    if out:
        os.replace(tmp, path)
    return changed, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    groups = fm.group_entries(fm.load_urls(fm.URLS_FILE))
    sources = [f for _, f, _ in groups if os.path.exists(os.path.join(OUTPUT_DIR, f"{f}.xml"))]
    targets = [s for s in sources if s in TARGETS]
    if not targets:
        print("No target EPG found, nothing to do.", file=sys.stderr)
        return
    idx = build_index([s for s in sources if s not in TARGETS])
    for name in targets:
        changed, total = align(name, idx, args.dry_run)
        print(f"[{name}] aligned {changed} of {total} programmes")
    if args.dry_run:
        return
    c, p = merge_epgs([(n, os.path.join(OUTPUT_DIR, f"{n}.xml")) for n in sources],
                      os.path.join(OUTPUT_DIR, f"{fm.COMBINED_NAME}.xml"))
    print(f"[{fm.COMBINED_NAME}] EPG {c} channels, {p} programmes")


if __name__ == "__main__":
    main()
