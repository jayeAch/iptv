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
(filler, "to be announced", or the channel's own name) never count as evidence
on their own. The exception: a guide whose titles are just the channel's own
name ("SNL Vault" on every slot) is matched on times alone, as a fallback,
when the names are alike, at least WILDCARD_MIN programmes line up with the
same start/stop, and nothing on either side contradicts the other. The two
channels must also have alike names (NAME_RATIO, ignoring words like "HD", "TV",
"West"), so unrelated channels that merely air the same series at the same
times are not treated as duplicates.

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
TITLE_TOLERANCE = timedelta(minutes=5)  # looser drift allowed when the titles match too
MIN_EXTRA_MATCHES = 1                # other matching programmes that confirm a duplicate
TITLE_RATIO = 0.85                  # title similarity counted as the same title
CONFLICT_SHARE = 0.05                 # conflicts allowed per matching programme (isolated title mismatches)
CONFLICT_OVERLAP = timedelta(minutes=5)  # overlap with a different programme needed to count as a conflict
MAX_SUBTITLE_WORDS = 2               # extra words allowed when one title is the other plus a subtitle
SHORT_GUIDE = 3                      # guides with fewer programmes are kept last
WILDCARD_MIN = 3                     # time-only matches needed when one guide uses the channel name as title
NAME_RATIO = 0.8                     # channel-name similarity counted as the same channel
NAME_NOISE = {"hd", "sd", "uhd", "fhd", "tv", "channel", "the",
              "east", "west", "pacific", "eastern", "western"}  # ignored when comparing channel names
SAMSUNG_RE = re.compile(r"samsung|(?<![a-z0-9])stvp-", re.IGNORECASE)
GENERIC_TITLES = {
    "", "filler", "to be announced", "tba", "tbd", "programming",
    "no information available", "no programming", "off air", "sign off",
}


def title_key(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").casefold()).strip()


def name_key(s):
    """Channel name reduced to its distinctive letters/digits: lowercase, no
    punctuation, and none of the NAME_NOISE words (\"Comet TV\" -> \"comet\",
    \"TBS West\" -> \"tbs\")."""
    words = [w for w in re.findall(r"[a-z0-9]+", (s or "").casefold()) if w not in NAME_NOISE]
    return "".join(words) or re.sub(r"[^a-z0-9]", "", (s or "").casefold())


def initials(s):
    """First letters of each word (\"Great Entertainment Television\" -> \"get\")."""
    return "".join(w[0] for w in re.findall(r"[a-z0-9]+", (s or "").casefold()) if w != "the")


def names_alike(a_names, b_names):
    """True if any channel name of one side is the same as, a prefix of,
    NAME_RATIO-similar to, or the initials of any channel name of the other."""
    for an in a_names:
        for bn in b_names:
            a, b = name_key(an), name_key(bn)
            if not a or not b:
                continue
            if a == b:
                return True
            short, long_ = sorted((a, b), key=len)
            if len(short) >= 4 and long_.startswith(short):
                return True
            if SequenceMatcher(None, a, b).ratio() >= NAME_RATIO:
                return True
            if (len(a) >= 3 and a == initials(bn)) or (len(b) >= 3 and b == initials(an)):
                return True
    return False


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
        v[:] = sorted(set(v))   # some guides list a programme twice
    return progs


def titles_match(a, b):
    """Same title, tolerating the small wording differences between sources:
    spacing ("cell mate"/"cellmate"), a short subtitle ("the divergent series"
    vs "the divergent series insurgent"), or a typo. A title that is merely the
    start of a much longer one (a bare series name vs an episode title) is NOT
    a match: different regions often shuffle episodes of the same series."""
    if a == b:
        return True
    sa, sb = a.replace(" ", ""), b.replace(" ", "")
    if sa == sb:
        return True
    wa, wb = a.split(), b.split()
    short, long_ = sorted((wa, wb), key=len)
    if (len("".join(short)) >= 6 and long_[:len(short)] == short
            and len(long_) - len(short) <= MAX_SUBTITLE_WORDS):
        return True
    return SequenceMatcher(None, sa, sb).ratio() >= TITLE_RATIO


def same(a, b):
    """Same programme: titles match and start/stop agree within TITLE_TOLERANCE
    (guides from different sources drift a couple of minutes)."""
    return (abs(a[0] - b[0]) <= TITLE_TOLERANCE and abs(a[1] - b[1]) <= TITLE_TOLERANCE
            and titles_match(a[2], b[2]))


def usable(prog, names):
    return prog[2] not in GENERIC_TITLES and prog[2] not in names


def current(progs, names, now):
    for p in progs:
        if p[0] <= now < p[1] and usable(p, names):
            return p
    return None


def aligned(p, q):
    """Same start and stop within TOLERANCE, whatever the titles say."""
    return abs(p[0] - q[0]) <= TOLERANCE and abs(p[1] - q[1]) <= TOLERANCE


def on_grid(p):
    """Starts and stops on a whole 5-minute mark. Scheduled-TV feeds (and
    their time-shifted West copies) all line up on such marks by chance;
    off-grid times (09:08:00, 03:35:32) matching on two feeds are strong
    evidence they are one feed."""
    return all(t.second == 0 and t.minute % 5 == 0 for t in (p[0], p[1]))


def agree(p, q):
    """Same programme: same title and times, or the very same off-grid times
    with a different title (one source gives the series, the other the episode)."""
    return same(p, q) or (aligned(p, q) and not on_grid(p))


def overlaps(p, q):
    """A real overlap between different programmes: longer than CONFLICT_OVERLAP,
    so a couple of minutes of drift between two guides is not a contradiction."""
    return min(p[1], q[1]) - max(p[0], q[0]) > CONFLICT_OVERLAP


def compare(u, v, now):
    """Returns the number of matching programmes if u and v are the same feed,
    otherwise 0. They must air the same thing right now, and no programme on
    one may overlap a different programme on the other (this veto always applies,
    unless the conflicts are a tiny share of the matches). A channel whose guide ends early, or
    lists only the current programme, is still a duplicate if nothing conflicts."""
    if not names_alike(u["names"], v["names"]):
        return 0
    cu, cv = current(u["progs"], u["names"], now), current(v["progs"], v["names"], now)
    if not cu or not cv or not agree(cu, cv):
        return compare_wild(u, v, now)
    names = u["names"] | v["names"]
    up = [p for p in u["progs"] if usable(p, names)]
    vp = [q for q in v["progs"] if usable(q, names)]
    extra = sum(1 for p in up if p is not cu and any(agree(p, q) for q in vp))
    conflicts = sum(1 for p in up for q in vp
                    if p is not cu and q is not cv and overlaps(p, q) and not agree(p, q))
    if conflicts > int(extra * CONFLICT_SHARE):
        return compare_wild(u, v, now)
    return 1 + extra


def wild(prog, names):
    """Placeholder that is the channel's own name: says nothing about what is
    airing, so it matches any programme with the same start/stop."""
    return prog[2] in names and prog[2] not in GENERIC_TITLES


def agree_w(p, q, names):
    return agree(p, q) or (aligned(p, q) and (wild(p, names) or wild(q, names)))


def current_w(progs, now):
    for p in progs:
        if p[0] <= now < p[1] and p[2] not in GENERIC_TITLES:
            return p
    return None


def compare_wild(u, v, now):
    """Fallback for guides that use the channel's own name as every title.
    Needs alike names, the same programme slot airing right now, at least
    WILDCARD_MIN aligned programmes (at least one involving a placeholder
    title), and zero overlapping-but-different programmes."""
    if not names_alike(u["names"], v["names"]):
        return 0
    names = u["names"] | v["names"]
    cu, cv = current_w(u["progs"], now), current_w(v["progs"], now)
    if not cu or not cv or not agree_w(cu, cv, names):
        return 0
    up = [p for p in u["progs"] if p[2] not in GENERIC_TITLES]
    vp = [q for q in v["progs"] if p_ok(q)]
    matched = [p for p in up if any(agree_w(p, q, names) for q in vp)]
    if len(matched) < WILDCARD_MIN:
        return 0
    if not any(wild(p, names) or any(wild(q, names) and agree_w(p, q, names) for q in vp)
               for p in matched):
        return 0
    if any(overlaps(p, q) and not agree_w(p, q, names) for p in up for q in vp):
        return 0
    return len(matched)


def p_ok(q):
    return q[2] not in GENERIC_TITLES


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
