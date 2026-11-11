#!/usr/bin/env python3
"""Unified M3U/EPG playlist normalizer and category engine.

Combines category resolution rules and playlist/EPG rewriting into a single
standalone script.

Usage:
    python normalize_playlists.py                       # playlists/*.m3u in place
    python normalize_playlists.py playlists/vizio.m3u   # specific files
    python normalize_playlists.py --epg                 # also fix XMLTV <category>
    python normalize_playlists.py --dry-run             # report only

How a channel's category is decided (first hit wins):
    1. NAME_OVERRIDES   exact channel-name -> category
    2. NAME_PINS        whole-word channel/show names (edit these to force a category)
    3. RAW_GROUPS       exact match on the source playlist's group-title
    4. RAW_KEYWORD_RULES   keyword inside the source group-title
    5. NAME_KEYWORD_RULES  keyword inside the channel name
    6. DEFAULT_CATEGORY
All rules emit final category names directly (no separate merge pass).
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# Final output categories (what ends up in group-title)
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
    'Food & Travel',
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

DEFAULT_CATEGORY = 'Entertainment'

# ---------------------------------------------------------------------------
# 1. NAME OVERRIDES -- exact (lowercased) channel name -> category
# ---------------------------------------------------------------------------
NAME_OVERRIDES: dict[str, str] = {
    'comedia': 'Comedy',
    'komedi': 'Comedy',
    'sport': 'Sports',
}

# ---------------------------------------------------------------------------
# 2. NAME PINS -- force a category by channel/show name.
# One phrase per line, no quotes or commas needed (apostrophes are fine).
# Checked BEFORE the raw group lookup, so they override whatever group the
# source playlist (or a previous run) put the channel in.
# Matching is case/accent-insensitive on WHOLE words/phrases, so 'ghost hunt'
# does not match 'ghost hunters' (list both).
# Order matters: first matching list wins. Paranormal stays on top; True Crime
# must stay above Crime.
# ---------------------------------------------------------------------------
def _fold_accents(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c))


_APOSTROPHES = re.compile(r"['\u2018\u2019\u02bc`\u00b4]")


def _norm(text: str) -> str:
    """Lowercase, strip accents and apostrophes (straight or curly) for pin matching."""
    return _APOSTROPHES.sub('', _fold_accents(text or '').lower())


def _words(block: str) -> tuple[str, ...]:
    return tuple(w.strip() for w in block.strip().splitlines() if w.strip())


NAME_PINS: list[tuple[str, tuple[str, ...]]] = [
    ('Paranormal', _words("""
        pluto tv overnaturlig
        a haunting
        alien files
        alien nation
        ancient aliens
        bigfoot
        destination truth
        fear zone
        ghost adventures
        ghost brothers
        ghost dimension
        ghost files
        ghost hunt
        ghost hunters
        ghost stories
        haunt tv
        haunted collector
        haunttv
        i was haunted
        long island medium
        monsterquest
        monsters are real
        most haunted
        paranormal
        psychic kids
        the dead files
        the unexplained
        ufo files
        ufo hunters
        unexplained files
        unexplained mysteries
    """)),
    ('True Crime', _words("""
        20/20
        american justice
        body cam
        cold case files
        cold justice
        cops
        court tv
        crime 24/7
        crimes cults killers
        crime files channel
        crime scenes
        crime stories
        crime up close
        crime watch daily
        crime zone
        dateline
        detective
        first 48
        forensic
        i (almost) got away with it
        investigation discovery
        killer mysteries
        law & crime
        law and crime
        live pd
        locked up abroad
        love kills
        murder files
        pluto tv crime
        snapped
        the new detectives
        to catch a smuggler
        true crime
        unsolved mysteries
        world's most evil killers
        world's wildest police videos
        xumo free crime tv
    """)),
    ('Crime', _words("""
        cagney & lacey
        columbo
        crime
        criminal minds
        csi
        diagnosis murder
        hawaii five-0
        hawaii five-o
        in the heat of the night
        law & order
        law and order
        matlock
        midsomer murders
        miss marple
        monk
        murder she wrote
        murder, she wrote
        mystery
        ncis
        nypd blue
        perry mason
        poirot
        rockford files
        sherlock
        whodunit
    """)),
    # Sci-Fi above Horror: anything both is Sci-Fi. These sit above the raw
    # group lookup so channels sourced under a generic "Movies" group still
    # land in their genre.
    ('Sci-Fi', _words("""
        dark matter tv
        fantasy & horror
        pluto tv fantastic
        sci-fi
        sci fi
        scifi
        science fiction
        stargate
        star trek
    """)),
    ('Horror', _words("""
        dread tv
        frightflix
        horror
        pluto tv terror
        scare o ween
        scares
        scream factory
        screambox
        shudder
        watch it scream
    """)),
]


def _compile_pins() -> list[tuple[str, re.Pattern]]:
    out = []
    for label, words in NAME_PINS:
        alts = '|'.join(re.escape(_norm(w)) for w in sorted(words, key=len, reverse=True))
        out.append((label, re.compile(r'(?<![a-z0-9])(?:' + alts + r')(?![a-z0-9])')))
    return out


_NAME_PIN_RES = _compile_pins()

# ---------------------------------------------------------------------------
# 3. RAW GROUPS -- exact (lowercased) source group-title -> category.
# Written category -> groups so each category's aliases sit together.
# ---------------------------------------------------------------------------
RAW_GROUPS: dict[str, tuple[str, ...]] = {
    'Ambiance': (
        'ambiance', 'mood + ambiance',
    ),
    'Anime & Gaming': (
        'anime', 'anime & animation', 'anime & gaming', 'animation & anime',
        'gaming', 'gaming & anime',
    ),
    'Automotive': (
        'auto & motor',
    ),
    'Classic TV': (
        'classic', 'classic tv', 'classic tv & movies', 'classics', 'nostalgia',
        'retro', 'vintage tv', 'western', 'westerns', 'westerns + classics',
    ),
    'Comedy': (
        'comedia', 'humor', 'komedi', 'komedie', 'sitcoms',
    ),
    'Crime': (
        'crime', 'crime + thriller', 'crime drama', 'crime dramas', 'crime tv',
        'crimen', 'mysteries', 'mystery', 'mystery & suspense', 'police dramas',
    ),
    'Documentary': (
        'biographies', 'biography', 'docs', 'documentaires', 'documentales',
        'documentari', 'documentaries', 'documentary & biography',
        'dokus & wissen', 'education', 'educational', 'factual', 'history',
        'history + docs', 'infotainment', 'science', 'special interest',
        'true stories',
    ),
    'Drama': (
        'asian drama', 'drama series', 'dramas', 'k-drama', 'romance', 'serie',
        'serier', 'series', 'soap operas', 'soaps', 'tv dramas',
    ),
    'Entertainment': (
        'action', 'action & adventure', 'action & drama', 'action and adventure',
        'action/adventure', 'adventure', 'bingeworthy', 'creators',
        'divertissement', 'entertainment', 'entretenimiento', 'featured',
        'general', 'intrattenimento', 'martial arts', 'new on pluto tv',
        'real life adventure', 'tv', 'tv & entertainment', 'tv & movies',
        'tv shows',
    ),
    'Faith': (
        'devotional', 'inspiration + faith',
    ),
    'Food & Travel': (
        'cucina & viaggi', 'food', 'food & travel', 'food + travel',
        'travel', 'travel & lifestyle',
    ),
    'Horror': (
        'horror', 'scary',
    ),
    'Kids': (
        'bambini', 'jeunesse', 'kids', 'kids + family', 'niños',
    ),
    'Latino': (
        'en español', 'latino',
    ),
    'Lifestyle': (
        'culture + lifestyle', 'home', 'home & diy', 'home & food', 'lifestyle',
        'shopping',
    ),
    'Local News': (
        'local channels', 'local news', 'regional news',
    ),
    'Movies': (
        'cine', 'cinéma', 'film', 'filme', 'filmer', 'movie channels', 'movies',
    ),
    'Music': (
        'musica', 'música', 'musik', 'musique',
    ),
    'Nature': (
        'nature', 'nature + science',
    ),
    'News': (
        'actualités', 'business', 'news', 'news + opinion', 'noticias', 'nyheter',
    ),
    'Paranormal': (
        'ghost hunting', 'ghosts', 'paranormal', 'paranormal & unexplained',
        'paranormal + unexplained', 'paranormal tv', 'ufo', 'ufos',
        'ufos & aliens', 'unexplained',
    ),
    'Reality TV': (
        'daytime tv & games', 'game shows', 'reality', 'reality tv',
    ),
    'Sci-Fi': (
        'adrenaline & sci-fi', 'fantasy', 'horror & sci-fi', 'sci fi', 'scifi',
        'sci-fi', 'sci-fi & fantasy', 'sci-fi & horror', 'sci-fi & supernatural',
        'sci-fi + fantasy', 'sci-fi and horror', 'science fiction',
    ),
    'Sports': (
        'calcio', 'deporte', 'outdoors', 'sport', 'sports',
    ),
    'True Crime': (
        'cold cases', 'detective', 'forensics', 'investigation', 'investigations',
        'real crime', 'true crime', 'true crime tv',
    ),
}

_CANONICAL_MAP: dict[str, str] = {
    group: label for label, groups in RAW_GROUPS.items() for group in groups
}
assert len(_CANONICAL_MAP) == sum(map(len, RAW_GROUPS.values())), \
    'RAW_GROUPS: a group name is listed under two categories'

_CANONICAL_LOWER: dict[str, str] = {c.lower(): c for c in CANONICAL_CATEGORIES}

# ---------------------------------------------------------------------------
# 4. RAW KEYWORD RULES -- substring of the source group-title (fallback when
# the group is not in RAW_GROUPS). First match wins.
#   - Local News precedes News.
#   - Paranormal precedes Sci-Fi (which owns the 'supernatural' keyword).
#   - Sci-Fi precedes Horror (so anything both is Sci-Fi).
#   - True Crime precedes Crime.
# ---------------------------------------------------------------------------
RAW_KEYWORD_RULES: list[tuple[str, tuple[str, ...]]] = [
    ('Local News',     ('local', 'regional')),
    ('Paranormal',     ('paranormal', 'unexplained')),
    ('Sci-Fi',         ('sci-fi', 'scifi', 'sci fi', 'science fiction', 'supernatural', 'fantasy')),
    ('Horror',         ('horror', 'scary')),
    ('Comedy',         ('comedy', 'komedi', 'humor', 'sitcom', 'stand-up')),
    ('True Crime',     ('true crime', 'real crime', 'forensic', 'detective', 'cold case', 'investigat')),
    ('Crime',          ('crime', 'mystery', 'thriller')),
    ('News',           ('news', 'business', 'finance', 'weather')),
    ('Movies',         ('movie', 'film', 'cinema')),
    ('Kids',           ('kid', 'family', 'cartoon')),
    ('Sports',         ('sport', 'soccer', 'football', 'basketball')),
    ('Music',          ('music', 'radio')),
    ('Reality TV',     ('game show', 'daytime', 'reality', 'dating')),
    ('Anime & Gaming', ('anime', 'gaming')),
    ('Food & Travel',  ('food', 'cook', 'travel')),
    ('Documentary',    ('doc', 'science', 'history', 'biograph', 'educat')),
    ('Drama',          ('drama', 'soap', 'romance', 'telenovela')),
    ('Nature',         ('nature', 'wildlife', 'animal')),
    ('Faith',          ('faith', 'relig', 'gospel', 'church')),
    ('Classic TV',     ('classic', 'retro', 'vintage', 'nostalgi', 'western')),
    ('Ambiance',       ('ambian', 'relax', 'fireplace')),
    ('Lifestyle',      ('lifestyle', 'shopping', 'fashion', 'home')),
    ('Latino',         ('latino', 'latin', 'espa')),
]

# ---------------------------------------------------------------------------
# 5. NAME KEYWORD RULES -- substring of the channel name (last resort before
# the default). First match wins. Trailing spaces are intentional.
#   - Local News precedes News.
#   - True Crime precedes Crime (so "law & crime" is True Crime).
#   - Sci-Fi precedes Horror (so anything both is Sci-Fi).
# ---------------------------------------------------------------------------
NAME_KEYWORD_RULES: list[tuple[str, tuple[str, ...]]] = [
    ('Sports',         ('sport', 'deportes', 'nfl', 'nba ', 'nhl', 'mlb', 'nascar', 'ufc', 'mma')),
    ('Music',          ('iheart', 'vevo', 'stingray', 'mtv ', 'concerts')),
    ('Ambiance',       ('ambient', 'fireplace', 'yule log', 'relaxation', 'lofi', 'slow tv')),
    ('Local News',     ('local news', 'fox local')),
    ('News',           ('news', 'noticias', 'weather', 'cnn', 'usa today', 'bloomberg')),
    ('True Crime',     ('true crime', 'court tv', 'cops', 'law & crime', 'forensic', 'dateline',
                        'first 48', 'cold case', 'investigat', 'detective')),
    ('Paranormal',     ('paranormal', 'ghost hunt', 'haunted', 'bigfoot', 'ufo ', 'ancient aliens',
                        'unexplained')),
    ('Crime',          ('crime', 'mystery', 'murder', 'whodunit')),
    ('Sci-Fi',         ('sci-fi', 'star trek', 'stargate', 'science fiction')),
    ('Horror',         ('horror', 'scary', 'screambox', 'haunt')),
    ('Anime & Gaming', ('anime', 'crunchyroll', 'retrocrush', 'yu-gi-oh')),
    ('Food & Travel',  ('food network', 'tastemade', 'cooking', 'kitchen', 'chef')),
    ('Nature',         ('nature', 'wildlife', 'wildearth', 'love nature', 'national geographic')),
    ('Lifestyle',      ('this old house', 'home & diy', 'gardening', 'home improvement')),
    ('Comedy',         ('comedy', 'laugh', 'sitcom', 'snl', 'mst3k', 'failarmy')),
    ('Kids',           ('kids', 'family', 'children', 'cartoon', 'junior')),
    ('Drama',          ('drama', 'hallmark', 'law & order', 'csi', 'ncis')),
    ('Movies',         ('movies', 'movie', 'cinema', 'film', 'paramount movie')),
    ('Classic TV',     ('western', 'gunsmoke', 'wild west', 'bonanza')),
    ('Food & Travel',  ('travel', 'journy', 'travelxp')),
    ('Documentary',    ('history', 'smithsonian', 'science', 'mythbusters', 'nasa+', 'space')),
    ('Lifestyle',      ('shopping', 'qvc', 'hsn', 'shop lc')),
]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _first_match(rules: list[tuple[str, tuple[str, ...]]], text: str) -> str | None:
    for label, keywords in rules:
        if any(kw in text for kw in keywords):
            return label
    return None


def pinned_category(name: str) -> str | None:
    text = _norm(name)
    for label, rx in _NAME_PIN_RES:
        if rx.search(text):
            return label
    return None


@lru_cache(maxsize=4096)
def _resolve_key(key: str) -> str | None:
    key = ' '.join(key.split())
    for variant in (key, _fold_accents(key)):
        hit = _CANONICAL_MAP.get(variant) or _CANONICAL_LOWER.get(variant)
        if hit:
            return hit
    return None


def normalize_category(raw: str | None) -> str | None:
    if not raw:
        return raw
    cleaned = re.sub(r'\s+presented\s+by\s+.+$', '', raw.strip(), flags=re.IGNORECASE).strip()
    cleaned = re.sub(r'\s*\([A-Za-z]{2}\)\s*$', '', cleaned).strip()
    return _resolve_key(cleaned.lower())


def infer_category_from_raw(raw: str | None) -> str | None:
    return _first_match(RAW_KEYWORD_RULES, _fold_accents(raw).lower()) if raw else None


def infer_category_from_name(title: str) -> str | None:
    return _first_match(NAME_KEYWORD_RULES, title.lower())


def category_for_channel(name: str, raw_category: str | None, source_name: str | None = None) -> str | None:
    """Category as it will appear in the output playlist. (source_name is unused; kept for callers.)"""
    name_clean = re.sub(r'(?:\s*\([A-Z]{2}\)|\s*\[DRM\])+\s*$', '', (name or '').strip()).strip()
    name_lower = name_clean.lower()

    return (
        NAME_OVERRIDES.get(name_lower)
        or pinned_category(name_clean)          # pins beat the raw group and earlier runs
        or normalize_category(raw_category)
        or infer_category_from_raw(raw_category)
        or infer_category_from_name(name_lower)
        or DEFAULT_CATEGORY                     # no odd raw group is ever left behind
    )


final_category = category_for_channel


# ---------------------------------------------------------------------------
# M3U / EPG rewriting
# ---------------------------------------------------------------------------
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
        new = category_for_channel(name, old) or old
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
