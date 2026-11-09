#!/usr/bin/env python3
"""Unified M3U/EPG playlist normalizer and category engine.

Combines category resolution rules and playlist/EPG rewriting into a single
standalone script.

Usage:
    python normalize_playlists.py                       # playlists/*.m3u in place
    python normalize_playlists.py playlists/vizio.m3u   # specific files
    python normalize_playlists.py --epg                 # also fix XMLTV <category>
    python normalize_playlists.py --dry-run             # report only
"""
from __future__ import annotations

import argparse
import html
import logging
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from functools import lru_cache
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical categories & Mapping dictionaries (from category_utils)
# ---------------------------------------------------------------------------
CANONICAL_CATEGORIES: tuple[str, ...] = (
    'Action & Adventure',
    'Ambiance',
    'Anime',
    'Automotive',
    'Broadcast',
    'Classic TV',
    'Comedy',
    'Documentary',
    'Drama',
    'Entertainment',
    'Faith',
    'Food',
    'Game Shows',
    'Gaming',
    'History',
    'Home & DIY',
    'Horror',
    'International',
    'Kids',
    'Latino',
    'Lifestyle',
    'Local News',
    'Movies',
    'Music',
    'Nature',
    'News',
    'Outdoors',
    'Reality TV',
    'Sci-Fi',
    'Science',
    'Shopping',
    'Sports',
    'Travel',
    'True Crime',
    'Westerns',
)

_CANONICAL_MAP: dict[str, str] = {
    'action': 'Action & Adventure',
    'action & drama': 'Action & Adventure',
    'action/adventure': 'Action & Adventure',
    'adventure': 'Action & Adventure',
    'martial arts': 'Action & Adventure',
    'action & adventure': 'Action & Adventure',
    'action and adventure': 'Action & Adventure',
    
    'anime & gaming': 'Anime',
    'gaming & anime': 'Anime',
    'anime & animation': 'Anime',
    'animation & anime': 'Anime',

    'classics': 'Classic TV',
    'classic': 'Classic TV',
    'classic tv': 'Classic TV',
    'classic tv & movies': 'Classic TV',
    'retro': 'Classic TV',
    'nostalgia': 'Classic TV',
    'vintage tv': 'Classic TV',

    'documentaries': 'Documentary',
    'factual': 'Documentary',
    'docs': 'Documentary',
    'documentary & biography': 'Documentary',
    'biography': 'Documentary',
    'biographies': 'Documentary',
    'true stories': 'Documentary',
    'special interest': 'Documentary',
    'educational': 'Documentary',
    'education': 'Documentary',

    'tv dramas': 'Drama',
    'dramas': 'Drama',
    'drama series': 'Drama',
    'soap operas': 'Drama',
    'soaps': 'Drama',
    'series': 'Drama',
    'k-drama': 'Drama',
    'asian drama': 'Drama',
    'romance': 'Drama',

    # Vizio WatchFree+ compound categories
    'kids + family': 'Kids',
    'food + travel': 'Food',
    'nature + science': 'Nature',
    'history + docs': 'History',
    'inspiration + faith': 'Faith',
    'westerns + classics': 'Classic TV',
    'news + opinion': 'News',
    'crime + thriller': 'True Crime',
    'mood + ambiance': 'Ambiance',
    'culture + lifestyle': 'Lifestyle',
    'en español': 'Latino',
    'home': 'Home & DIY',
    'featured': 'Entertainment',
    'creators': 'Entertainment',

    # International / Multilingual variants
    'actualités': 'News',
    'auto & motor': 'Automotive',
    'bambini': 'Kids',
    'calcio': 'Sports',
    'cine': 'Movies',
    'cinéma': 'Movies',
    'comedia': 'Comedy',
    'komedi': 'Comedy',
    'crime': 'True Crime',
    'crimen': 'True Crime',
    'cucina & viaggi': 'Food',
    'deporte': 'Sports',
    'devotional': 'Faith',
    'divertissement': 'Entertainment',
    'documentaires': 'Documentary',
    'documentales': 'Documentary',
    'documentari': 'Documentary',
    'dokus & wissen': 'Documentary',
    'entertainment': 'Entertainment',
    'entretenimiento': 'Entertainment',
    'film': 'Movies',
    'filme': 'Movies',
    'filmer': 'Movies',
    'food & travel': 'Food',
    'game shows': 'Game Shows',
    'home & food': 'Lifestyle',
    'infotainment': 'Documentary',
    'intrattenimento': 'Entertainment',
    'jeunesse': 'Kids',
    'kids': 'Kids',
    'latino': 'Latino',
    'lifestyle': 'Lifestyle',
    'movies': 'Movies',
    'musica': 'Music',
    'musik': 'Music',
    'musique': 'Music',
    'música': 'Music',
    'nature': 'Nature',
    'news': 'News',
    'niños': 'Kids',
    'noticias': 'News',
    'nyheter': 'News',
    'reality': 'Reality TV',
    'reality tv': 'Reality TV',
    'regional news': 'Local News',
    'serie': 'Drama',
    'serier': 'Drama',
    'sport': 'Sports',
    'sports': 'Sports',
    'travel & lifestyle': 'Travel',
    'true crime': 'True Crime',
    'western': 'Westerns',
    'westerns': 'Westerns',
}

_CANONICAL_LOWER: dict[str, str] = {}


def _fold_accents(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c))


def _key_variants(key: str) -> list[str]:
    key = ' '.join(key.split())
    variants = [key]
    for v in list(variants):
        folded = _fold_accents(v)
        if folded not in variants:
            variants.append(folded)
    return variants


def _lookup_exact(key: str) -> str | None:
    if key in _CANONICAL_MAP:
        return _CANONICAL_MAP[key]
    if not _CANONICAL_LOWER:
        _CANONICAL_LOWER.update({c.lower(): c for c in CANONICAL_CATEGORIES})
    return _CANONICAL_LOWER.get(key)


@lru_cache(maxsize=4096)
def _resolve_key(key: str) -> str | None:
    for variant in _key_variants(key):
        hit = _lookup_exact(variant)
        if hit:
            return hit
    return None


def normalize_category(raw: str | None) -> str | None:
    if not raw:
        return raw
    cleaned = re.sub(r'\s+presented\s+by\s+.+$', '', raw.strip(), flags=re.IGNORECASE).strip()
    cleaned = re.sub(r'\s*\([A-Za-z]{2}\)\s*$', '', cleaned).strip()
    key = cleaned.lower()
    return _resolve_key(key)


_NAME_CATEGORY_RULES: list[tuple[set[str], str]] = [
    ({'sport', 'deportes', 'nfl', 'nba ', 'nhl', 'mlb', 'nascar', 'ufc', 'mma'}, 'Sports'),
    ({'iheart', 'vevo', 'stingray', 'mtv ', 'concerts'}, 'Music'),
    ({'ambient', 'fireplace', 'yule log', 'relaxation', 'lofi', 'slow tv'}, 'Ambiance'),
    ({'news', 'noticias', 'weather', 'cnn', 'fox local', 'usa today', 'bloomberg'}, 'News'),
    ({'crime', 'mystery', 'court tv', 'cops', 'law & crime', 'forensic files'}, 'True Crime'),
    ({'horror', 'scary', 'screambox', 'haunt', 'fear zone'}, 'Horror'),
    ({'sci-fi', 'star trek', 'stargate', 'science fiction'}, 'Sci-Fi'),
    ({'anime', 'crunchyroll', 'retrocrush', 'yu-gi-oh'}, 'Anime'),
    ({'food network', 'tastemade', 'cooking', 'kitchen', 'chef'}, 'Food'),
    ({'nature', 'wildlife', 'wildearth', 'love nature', 'national geographic'}, 'Nature'),
    ({'this old house', 'home & diy', 'gardening', 'home improvement'}, 'Home & DIY'),
    ({'comedy', 'laugh', 'sitcom', 'snl', 'mst3k', 'failarmy'}, 'Comedy'),
    ({'kids', 'family', 'children', 'cartoon', 'junior'}, 'Kids'),
    ({'drama', 'hallmark', 'law & order', 'csi', 'ncis'}, 'Drama'),
    ({'movies', 'movie', 'cinema', 'film', 'paramount movie'}, 'Movies'),
    ({'western', 'gunsmoke', 'wild west', 'bonanza'}, 'Westerns'),
    ({'travel', 'adventure', 'journy', 'travelxp'}, 'Travel'),
    ({'history', 'smithsonian', 'ancient aliens'}, 'History'),
    ({'science', 'mythbusters', 'nasa+', 'space'}, 'Science'),
    ({'shopping', 'qvc', 'hsn', 'shop lc'}, 'Shopping'),
]

_NAME_OVERRIDES: dict[str, str] = {
    'komedi': 'Comedy',
    'comedia': 'Comedy',
    'sport': 'Sports',
}


def infer_category_from_name(title: str) -> str | None:
    tl = title.lower()
    for keywords, label in _NAME_CATEGORY_RULES:
        if any(kw in tl for kw in keywords):
            return label
    return None


def category_for_channel(name: str, raw_category: str | None, source_name: str | None = None) -> str | None:
    name_clean = re.sub(r'(?:\s*\([A-Z]{2}\)|\s*\[DRM\])+\s*$', '', (name or '').strip()).strip()
    name_lower = name_clean.lower()

    if name_lower in _NAME_OVERRIDES:
        return _NAME_OVERRIDES[name_lower]

    normalized = normalize_category(raw_category)
    if normalized:
        return normalized

    return infer_category_from_name(name_lower)


# ---------------------------------------------------------------------------
# Final player-facing group consolidation pass
# ---------------------------------------------------------------------------
MERGE: dict[str, str] = {
    "Outdoors": "Sports",
    "Shopping": "Lifestyle",
    "Home & DIY": "Lifestyle",
    "Gaming": "Anime & Gaming",
    "Anime": "Anime & Gaming",
    "Science": "Documentary",
    "History": "Documentary",
    "Sci-Fi": "Sci-Fi & Horror",
    "Horror": "Sci-Fi & Horror",
    "Westerns": "Classic TV",
    "Food": "Food & Travel",
    "Travel": "Food & Travel",
    "Action & Adventure": "Drama",
    "Game Shows": "Reality TV",
}

GROUP_RE = re.compile(r'group-title="([^"]*)"')
TVG_ID_RE = re.compile(r'tvg-id="([^"]*)"')


def _split_extinf(line: str) -> tuple[str, str]:
    in_q = False
    last = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ',' and not in_q:
            last = i
    return (line[:last], line[last + 1:].strip()) if last >= 0 else (line, "")


def normalize_m3u(path: Path, dry_run: bool) -> tuple[dict[str, dict[str, str]], Counter]:
    source = path.stem.lower()
    out: list[str] = []
    by_id: dict[str, dict[str, str]] = {}
    changes: Counter = Counter()

    for line in path.read_text(encoding="utf-8").splitlines():
        m = GROUP_RE.search(line) if line.startswith("#EXTINF") else None
        if not m:
            out.append(line)
            continue
        old = html.unescape(m.group(1))
        attrs, name = _split_extinf(line)
        new = category_for_channel(name, old, source) or old
        new = MERGE.get(new, new)
        if new != old:
            changes[(old, new)] += 1
            line = GROUP_RE.sub(lambda _m: f'group-title="{new.replace(chr(34), "&quot;")}"', line, count=1)
        idm = TVG_ID_RE.search(attrs)
        if idm:
            by_id[idm.group(1)] = {"old": old, "new": new}
        out.append(line)

    if not dry_run:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return by_id, changes


def normalize_epg(path: Path, by_id: dict[str, dict[str, str]], dry_run: bool) -> int:
    tree = ET.parse(path)
    n = 0
    for prog in tree.getroot().iter("programme"):
        info = by_id.get(prog.get("channel", ""))
        if not info or info["old"] == info["new"]:
            continue
        for cat in prog.findall("category"):
            if (cat.text or "").strip() == info["old"]:
                cat.text = info["new"]
                n += 1
    if n and not dry_run:
        tree.write(path, encoding="utf-8", xml_declaration=True)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path, help="M3U files (default: playlists/*.m3u)")
    ap.add_argument("--epg", action="store_true", help="also rewrite matching <category> in the sibling .xml")
    ap.add_argument("--dry-run", action="store_true", help="report changes without writing")
    args = ap.parse_args()

    files = args.files or sorted(Path("playlists").glob("*.m3u"))
    if not files:
        print("no playlists found", file=sys.stderr)
        return 1

    for f in files:
        if not f.is_file():
            print(f"skip {f}: not found", file=sys.stderr)
            continue
        by_id, changes = normalize_m3u(f, args.dry_run)
        total = sum(changes.values())
        print(f"{f}: {total} group-title change(s)")
        for (old, new), c in changes.most_common(8):
            print(f"    {c:4d}  {old!r} -> {new!r}")
        xml = f.with_suffix(".xml")
        if args.epg and xml.is_file():
            print(f"{xml}: {normalize_epg(xml, by_id, args.dry_run)} <category> change(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
