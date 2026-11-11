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
# Final output categories (what ends up in group-title after MERGE)
#   Crime      = fictional crime dramas / mysteries
#   True Crime = real crime: forensics, investigations, detective work, court
#   Paranormal = shows presented as real supernatural events (ghost hunts,
#                hauntings, UFOs, cryptids); NOT supernatural dramas/fiction,
#                which stay in Sci-Fi / Horror / Drama
#   Sci-Fi and Horror are separate; anything both defaults to Sci-Fi
# ---------------------------------------------------------------------------
CANONICAL_CATEGORIES: tuple[str, ...] = (
    'Ambiance',
    'Anime & Gaming',
    'Automotive',
    'Broadcast',
    'Classic TV',
    'Comedy',
    'Crime',
    'Documentary',
    'Drama',
    'Entertainment',
    'Faith',
    'Travel',
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
    'Paranormal',
    'Reality TV',
    'Sci-Fi',
    'Sports',
    'True Crime',
)

# ---------------------------------------------------------------------------
# CHANNEL NAME PINS -- edit these lists to force a category by channel/show name.
# Checked BEFORE the raw group lookup, so they override whatever group the
# source playlist (or a previous run) put the channel in.
# Matching is case/accent-insensitive on whole words/phrases in the channel name.
# Order matters: first matching list wins. Paranormal stays on top; True Crime
# must stay above Crime.
# ---------------------------------------------------------------------------
_NAME_PINS: list[tuple[str, tuple[str, ...]]] = [
    ('Paranormal', (
        'paranormal', 'ghost hunters', 'ghost adventures', 'ghost hunt',
        'ghost stories', 'ghost brothers', 'ghost files', 'the dead files',
        'most haunted', 'haunted collector', 'i was haunted',
        'finding bigfoot', 'bigfoot', 'monsters are real', 'monsterquest',
        'destination truth', 'ancient aliens', 'ufo files', 'ufo hunters',
        'the unexplained', 'unexplained files', 'unexplained mysteries',
        'long island medium', 'psychic kids', 'ghost dimension',
        'alien files', 'a haunting', 'haunttv', 'haunt tv', 'alien nation', 'fear zone', 'fear zone [drm]',
    )),
    ('True Crime', (
        'love kills', 'crime up close', 'crime zone', 'to catch a smuggler', 'locked up abroad', 'true crime', 'forensic files', 'cold case files', 'dateline',
        'the first 48', 'first 48', 'cops', 'court tv', 'law & crime',
        'law and crime', 'investigation discovery', 'unsolved mysteries',
        'snapped', 'american justice', 'cold justice', 'live pd',
        'body cam', 'crime watch daily', 'murder files', 'crime stories',
        'forensic', 'detective', 'i (almost) got away with it', 'crime cults killers', 'crime scenes', 'pluto tv crime', '20/20', 'world's most evil killers', 'world's wildest police videos', 'crime files channel', 'crime 24/7', 'the new detectives', 'killer mysteries', 
    )),
    ('Crime', (
        'csi', 'law & order', 'law and order', 'ncis', 'criminal minds',
        'monk', 'columbo', 'perry mason', 'murder, she wrote',
        'murder she wrote', 'matlock', 'rockford files', 'cagney & lacey',
        'hawaii five-0', 'hawaii five-o', 'in the heat of the night',
        'diagnosis murder', 'midsomer murders', 'miss marple',
        'hercule poirot', 'poirot', 'sherlock', 'whodunit', 'mystery',
        'crime drama', 'crime',
    )),
]


def _compile_pins() -> list[tuple[str, re.Pattern]]:
    out = []
    for label, words in _NAME_PINS:
        alts = '|'.join(re.escape(w) for w in sorted(words, key=len, reverse=True))
        out.append((label, re.compile(r'(?<![a-z0-9])(?:' + alts + r')(?![a-z0-9])')))
    return out


_NAME_PIN_RES = _compile_pins()


def pinned_category(name: str) -> str | None:
    text = _fold_accents(name or '').lower()
    for label, rx in _NAME_PIN_RES:
        if rx.search(text):
            return label
    return None


# Values here may be pre-merge names; MERGE (below) folds them into final names.
_CANONICAL_MAP: dict[str, str] = {
    # Action / adventure: no dedicated category, uncategorizable -> Entertainment
    'action': 'Entertainment',
    'action & drama': 'Entertainment',
    'action/adventure': 'Entertainment',
    'adventure': 'Entertainment',
    'martial arts': 'Entertainment',
    'action & adventure': 'Entertainment',
    'action and adventure': 'Entertainment',
    'real life adventure': 'Entertainment',

    # Pre-merge names seen as raw groups, pinned so they never depend on keywords
    'outdoors': 'Sports',
    'shopping': 'Lifestyle',
    'home & diy': 'Lifestyle',
    'gaming': 'Anime & Gaming',
    'anime': 'Anime & Gaming',
    'science': 'Documentary',
    'history': 'Documentary',
    'food': 'Food & Travel',
    'travel': 'Food & Travel',
    'ambiance': 'Ambiance',

    'anime & gaming': 'Anime & Gaming',
    'gaming & anime': 'Anime & Gaming',
    'anime & animation': 'Anime & Gaming',
    'animation & anime': 'Anime & Gaming',

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
    'food + travel': 'Travel',
    'nature + science': 'Nature',
    'history + docs': 'Documentary',
    'inspiration + faith': 'Faith',
    'westerns + classics': 'Classic TV',
    'news + opinion': 'News',
    'crime + thriller': 'Crime',
    'mood + ambiance': 'Ambiance',
    'culture + lifestyle': 'Lifestyle',
    'en español': 'Latino',
    'home': 'Lifestyle',
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
    'crimen': 'Crime',
    'cucina & viaggi': 'Travel',
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
    'food & travel': 'Travel',
    'game shows': 'Reality TV',
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
    'western': 'Classic TV',
    'westerns': 'Classic TV',

    # Crime (fictional crime dramas / mysteries)
    'crime': 'Crime',
    'crime tv': 'Crime',
    'crime drama': 'Crime',
    'crime dramas': 'Crime',
    'police dramas': 'Crime',
    'mystery': 'Crime',
    'mysteries': 'Crime',
    'mystery & suspense': 'Crime',

    # True Crime (real crime: forensics, investigations, detective work)
    'true crime': 'True Crime',
    'true crime tv': 'True Crime',
    'real crime': 'True Crime',
    'forensics': 'True Crime',
    'investigation': 'True Crime',
    'investigations': 'True Crime',
    'detective': 'True Crime',
    'cold cases': 'True Crime',

    # Paranormal (purported real supernatural events, not fiction)
    'paranormal': 'Paranormal',
    'paranormal tv': 'Paranormal',
    'paranormal & unexplained': 'Paranormal',
    'paranormal + unexplained': 'Paranormal',
    'unexplained': 'Paranormal',
    'ghosts': 'Paranormal',
    'ghost hunting': 'Paranormal',
    'ufo': 'Paranormal',
    'ufos': 'Paranormal',
    'ufos & aliens': 'Paranormal',

    # Sci-Fi / Horror (separate; both together -> Sci-Fi)
    'sci-fi': 'Sci-Fi',
    'scifi': 'Sci-Fi',
    'sci fi': 'Sci-Fi',
    'fantasy': 'Sci-Fi',
    'sci-fi & fantasy': 'Sci-Fi',
    'science fiction': 'Sci-Fi',
    'sci-fi + fantasy': 'Sci-Fi',
    'sci-fi & supernatural': 'Sci-Fi',
    'adrenaline & sci-fi': 'Sci-Fi',
    'horror & sci-fi': 'Sci-Fi',
    'sci-fi & horror': 'Sci-Fi',
    'sci-fi and horror': 'Sci-Fi',
    'horror': 'Horror',
    'scary': 'Horror',

    # Raw names seen in iptv playlists that previously fell through unchanged
    'tv shows': 'Entertainment',
    'tv': 'Entertainment',
    'tv & movies': 'Entertainment',
    'tv & entertainment': 'Entertainment',
    'general': 'Entertainment',
    'bingeworthy': 'Entertainment',
    'new on pluto tv': 'Entertainment',
    'daytime tv & games': 'Reality TV',
    'sitcoms': 'Comedy',
    'humor': 'Comedy',
    'komedie': 'Comedy',
    'movie channels': 'Movies',
    'business': 'News',
    'local channels': 'Local News',
    'local news': 'Local News',
}

_CANONICAL_LOWER: dict[str, str] = {c.lower(): c for c in CANONICAL_CATEGORIES}


def _fold_accents(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c))


def _key_variants(key: str) -> list[str]:
    key = ' '.join(key.split())
    variants = [key]
    folded = _fold_accents(key)
    if folded not in variants:
        variants.append(folded)
    return variants


def _lookup_exact(key: str) -> str | None:
    return _CANONICAL_MAP.get(key) or _CANONICAL_LOWER.get(key)


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


# Order matters: first match wins.
#   - Local News precedes News.
#   - True Crime precedes Crime (so "law & crime" is True Crime).
#   - Sci-Fi precedes Horror (so anything both is Sci-Fi).
_NAME_CATEGORY_RULES: list[tuple[set[str], str]] = [
    ({'sport', 'deportes', 'nfl', 'nba ', 'nhl', 'mlb', 'nascar', 'ufc', 'mma'}, 'Sports'),
    ({'iheart', 'vevo', 'stingray', 'mtv ', 'concerts'}, 'Music'),
    ({'ambient', 'fireplace', 'yule log', 'relaxation', 'lofi', 'slow tv'}, 'Ambiance'),
    ({'local news', 'fox local'}, 'Local News'),
    ({'news', 'noticias', 'weather', 'cnn', 'usa today', 'bloomberg'}, 'News'),
    ({'true crime', 'court tv', 'cops', 'law & crime', 'forensic', 'dateline',
      'first 48', 'cold case', 'investigat', 'detective'}, 'True Crime'),
    ({'paranormal', 'ghost hunt', 'haunted', 'bigfoot', 'ufo ', 'ancient aliens',
      'unexplained'}, 'Paranormal'),
    ({'crime', 'mystery', 'murder', 'whodunit'}, 'Crime'),
    ({'sci-fi', 'star trek', 'stargate', 'science fiction'}, 'Sci-Fi'),
    ({'horror', 'scary', 'screambox', 'haunt', 'fear zone'}, 'Horror'),
    ({'anime', 'crunchyroll', 'retrocrush', 'yu-gi-oh'}, 'Anime'),
    ({'food network', 'tastemade', 'cooking', 'kitchen', 'chef'}, 'Food'),
    ({'nature', 'wildlife', 'wildearth', 'love nature', 'national geographic'}, 'Nature'),
    ({'this old house', 'home & diy', 'gardening', 'home improvement'}, 'Home & DIY'),
    ({'comedy', 'laugh', 'sitcom', 'snl', 'mst3k', 'failarmy'}, 'Comedy'),
    ({'kids', 'family', 'children', 'cartoon', 'junior'}, 'Kids'),
    ({'drama', 'hallmark', 'law & order', 'csi', 'ncis'}, 'Drama'),
    ({'movies', 'movie', 'cinema', 'film', 'paramount movie'}, 'Movies'),
    ({'western', 'gunsmoke', 'wild west', 'bonanza'}, 'Westerns'),
    ({'travel', 'journy', 'travelxp'}, 'Travel'),
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


DEFAULT_CATEGORY = 'Entertainment'

# Order matters: first match wins.
#   - Local News precedes News.
#   - True Crime precedes Crime.
#   - Paranormal precedes Sci-Fi (which owns the 'supernatural' keyword).
#   - Sci-Fi precedes Horror (so anything both is Sci-Fi).
_RAW_KEYWORD_RULES: list[tuple[tuple[str, ...], str]] = [
    (('local', 'regional'), 'Local News'),
    (('paranormal', 'unexplained'), 'Paranormal'),
    (('sci-fi', 'scifi', 'sci fi', 'science fiction', 'supernatural', 'fantasy'), 'Sci-Fi'),
    (('horror', 'scary'), 'Horror'),
    (('comedy', 'komedi', 'humor', 'sitcom', 'stand-up'), 'Comedy'),
    (('true crime', 'real crime', 'forensic', 'detective', 'cold case', 'investigat'), 'True Crime'),
    (('crime', 'mystery', 'thriller'), 'Crime'),
    (('news', 'business', 'finance', 'weather'), 'News'),
    (('movie', 'film', 'cinema'), 'Movies'),
    (('kid', 'family', 'cartoon'), 'Kids'),
    (('sport', 'soccer', 'football', 'basketball'), 'Sports'),
    (('music', 'radio'), 'Music'),
    (('game show', 'daytime', 'reality', 'dating'), 'Reality TV'),
    (('anime', 'gaming'), 'Anime'),
    (('food', 'cook', 'travel'), 'Food'),
    (('doc', 'science', 'history', 'biograph', 'educat'), 'Documentary'),
    (('drama', 'soap', 'romance', 'telenovela'), 'Drama'),
    (('nature', 'wildlife', 'animal'), 'Nature'),
    (('faith', 'relig', 'gospel', 'church'), 'Faith'),
    (('classic', 'retro', 'vintage', 'nostalgi', 'western'), 'Classic TV'),
    (('ambian', 'relax', 'fireplace'), 'Ambiance'),
    (('lifestyle', 'shopping', 'fashion', 'home'), 'Lifestyle'),
    (('latino', 'latin', 'espa'), 'Latino'),
]


def infer_category_from_raw(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _fold_accents(raw).lower()
    for keywords, label in _RAW_KEYWORD_RULES:
        if any(kw in text for kw in keywords):
            return label
    return None


def category_for_channel(name: str, raw_category: str | None, source_name: str | None = None) -> str | None:
    name_clean = re.sub(r'(?:\s*\([A-Z]{2}\)|\s*\[DRM\])+\s*$', '', (name or '').strip()).strip()
    name_lower = name_clean.lower()

    if name_lower in _NAME_OVERRIDES:
        return _NAME_OVERRIDES[name_lower]

    # Channel-name pins win over the raw group (and over groups set by earlier runs).
    pinned = pinned_category(name_clean)
    if pinned:
        return pinned

    normalized = normalize_category(raw_category)
    if normalized:
        return normalized

    # Unknown raw group: try keywords in the raw group, then the channel name,
    # and finally a broad default so no odd raw group name is ever left behind.
    return (
        infer_category_from_raw(raw_category)
        or infer_category_from_name(name_lower)
        or DEFAULT_CATEGORY
    )


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
    "Westerns": "Classic TV",
    "Food": "Food & Travel",
    "Travel": "Food & Travel",
    "Game Shows": "Reality TV",
}

def final_category(name: str, raw_category: str | None, source_name: str | None = None) -> str | None:
    """Category as it will appear in the output playlist (resolved, then MERGE-folded)."""
    new = category_for_channel(name, raw_category, source_name)
    if not new:
        return new
    for _ in range(3):
        merged = MERGE.get(new, new)
        if merged == new:
            break
        new = merged
    return new


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
        new = final_category(name, old, source) or old
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
