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
#   Drama      = non-crime dramas (crime dramas go to Crime)
#   Ambiance   = ambient/wellness video channels (Stingray Naturescape etc.)
#   True Crime = real crime: forensics, investigations, detective work, court
#   Paranormal = shows presented as real supernatural events (ghost hunts,
#                hauntings, UFOs, cryptids); NOT supernatural dramas/fiction,
#                which stay in Sci-Fi / Horror / Drama
#   Sci-Fi and Horror are separate; anything both defaults to Sci-Fi
#   Sports / Outdoors / Kung Fu Flicks / Combat Sports = pinned by channel name (see NAME_PINS);
#                MMA/UFC/kickboxing/fight promotions go to Combat Sports, kung fu and
#                martial-arts movies to Kung Fu Flicks,
#                everything else athletic to Sports, hunting/fishing/adventure to Outdoors
#   Toons      = cartoons / animated series (incl. adult animation); sits above
#                Comedy and Kids so animated comedies land here
# ---------------------------------------------------------------------------
CANONICAL_CATEGORIES: tuple[str, ...] = (
    'Ambiance',
    'Anime & Gaming',
    'Automotive',
    'Broadcast',
    'Classic TV',
    'Combat Sports',
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
    'Kung Fu Flicks',
    'Latino',
    'Lifestyle',
    'Local News',
    'Movies',
    'Music',
    'Nature',
    'News',
    'Outdoors',
    'Paranormal',
    'Reality TV',
    'Sci-Fi',
    'Sports',
    'Toons',
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
    # Combat sports first so these beat the Kung Fu Flicks and Sports pins below.
    ('Combat Sports', _words("""
        mma
        ufc
        swerve combat
        fight network
        one championship
        one championship tv
        glory kickboxing
    """)),
    # Martial arts next so MMA/UFC/kickboxing beat the generic Sports pins below.
    ('Kung Fu Flicks', _words("""
        kung fu
        kungfu
        martial art
        martial arts
        wu tang collection
        wu-tang collection
        shaw brothers
        bruce lee
        jackie chan
        karate
        kickboxing
        taekwondo
        judo
        jiu jitsu
        jiu-jitsu
        muay thai
        fight club
        combatv
        united fight alliance
        hong kong fight club
    """)),
    ('Sports', _words("""
        sport
        sports
        sports channel
        espn
        fox sports
        cbs sports
        nbc sports
        sky sport
        sky sports
        bein sports
        bally sports
        dazn
        tsn
        tsn1
        tsn2
        nfl
        nba
        mlb
        nhl
        nascar
        nhra
        wwe
        aew
        watch aew
        pga tour
        golf
        golf channel
        golfpass
        casual golf
        dp world tour
        big ten network
        sec network
        acc network
        acc digital network
        accdn
        big 12 studios
        pac-12 insider
        nesn
        nesn nation
        msg
        yes network
        space city home network
        fanduel
        fanduel tv extra
        vsin
        vsport
        victory+
        overtime
        stadium
        tudn
        zona tudn
        fox deportes
        itv deportes
        gol tv
        canal+ sport
        uefa.tv
        fifa
        premier league
        man city tv
        real madrid
        barca 24h
        top barca
        lo mejor de liga
        k-baseball tv
        f1 channel
        motogp channel
        motorsport.tv
        racer international
        racer network
        racer select
        racing america
        rally tv
        fuel tv
        speed sport
        monster jam
        flohockey
        floracing 24/7
        willow cricket
        cricket gold
        rugby pass tv
        lacrosse tv
        pll network
        pickleballtv
        pickletv
        billiard
        billiards
        world billiards tv
        bowling tv
        snooker 900
        tennis
        strongman
        sumo
        surfer tv
        surf now tv
        surf cinema
        world surf league
        wrestling legends tv
        tna wrestling channel
        top rank classics
        boxing tv
        hbo boxing
        acl cornhole
        hard knocks
        the jim rome show
        generation iron
        pluto tv handboll
        football
        soccer
        baseball
        basketball
        hockey
        cricket
        rugby
        wrestling
        boxing
        pokergo
        poker go
        poker night
        triton poker
        world poker tour
        esports edge
        team liquid
        retake e-sport live
        american ninja warrior
        ninja warrior
        american gladiators
        american gladiators by mgm
    """)),
    ('Outdoors', _words("""
        outdoor
        all-out alaska
        outdoors
        outdoor channel
        outside
        outside tv
        pursuit channel
        pursuitup
        wired2fish
        duck hunting tv
        game & fish tv
        gone fishing
        hunt fish tv
        hunting
        fishing
        bassmaster
        xtreme outdoor by history
        wild tv
        great american adventures
        cowboy channel
        powernation
        survivorman
        life below zero
        inwild
        inwonder
    """)),
    # Stingray's ambient/wellness video channels (its music channels stay in Music).
    ('Ambiance', _words("""
        stingray naturescape
        naturescape
        zenlife
        zen life
        stingray stargaze
        stargaze
        stingray cozy cafe
        cozy cafe
        stingray cityscapes
        cityscapes
        stingray spa
    """)),
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
        60 days in jail
        20/20
        crime 360
        american justice
        body cam
        cold case files
        cold justice
        cops
        court tv
        crime 24/7
        crimes cults killers
        crime cults killers
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
        snapped
        the new detectives
        to catch a smuggler
        true crime
        unsolved mysteries
        world's most evil killers
        world's wildest police videos
        xumo free crime tv
    """)),
    # Must sit ABOVE Crime: the Crime list has the whole word 'mystery', which
    # would otherwise capture "Mystery Science Theater 3000".
    ('Comedy', _words("""
        mystery science theater 3000
        mystery science theater
        mst3k
    """)),
    ('Crime', _words("""
        cagney & lacey
        columbo
        pluto tv crime thrillers
        pluto tv crime drama
        pluto tv crime movies
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
    # Non-crime dramas (crime dramas stay in Crime above).
    ('Drama', _words("""
        more tv drama
        nip/tuck
        pluto tv drama
        teen wolf
        vikings
        7th heaven
        beverly hills 90210
        dawsons creek
        dr. quinn
        dynasty
        everwood
        falcon crest
        gilmore girls
        heartland
        highway to heaven
        knots landing
        little house on the prairie
        melrose place
        one tree hill
        peyton place
        st. elsewhere
        the waltons
        touched by an angel
        general hospital
        days of our lives
        the young and the restless
        the bold and the beautiful
    """)),
    # Sci-Fi above Horror: anything both is Sci-Fi. These sit above the raw
    # group lookup so channels sourced under a generic "Movies" group still
    # land in their genre.
    ('Sci-Fi', _words("""
        the asylum
        farscape
        outersphere
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
    # Toons above Comedy/Kids: animated shows (incl. adult animation) stay here.
    ('Toons', _words("""
        aqua teen hunger force
        archer
        american dad
        beavis and butt-head
        beavis and butthead
        bobs burgers
        boomerang
        cartoon
        cartoons
        cartoon network
        comedy central animation
        family guy
        futurama
        king of the hill
        looney tunes
        metalocalypse
        mr. pickles
        primal
        rick and morty
        robot chicken
        samurai jack
        south park
        spongebob
        squidbillies
        the simpsons
        the venture bros.
        toon goggles
        toonami
        toons
        tom and jerry
        your pretty face is going to hell
    """)),
    ('Comedy', _words("""
        comedy central
        comedy vault
        spin city
        dry bar comedy
        great comedy
        laff more
        sitcom
        sitcoms
        snl vault
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
    'Combat Sports': (
        'combat sports',
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
        'general', 'intrattenimento', 'new on pluto tv',
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
    'Kung Fu Flicks': (
        'kung fu', 'martial arts',
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
    'Outdoors': (
        'outdoor', 'outdoors',
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
    'Toons': (
        'animated', 'animation', 'cartoon', 'cartoons', 'toons',
    ),
    'Sports': (
        'calcio', 'deporte', 'motor sports', 'sport', 'sports', 'sports & outdoors',
        'sports on now',
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
    ('Combat Sports',  ('combat sport',)),
    ('Kung Fu Flicks', ('martial art', 'kung fu')),
    ('Outdoors',       ('outdoor',)),
    ('Local News',     ('local', 'regional')),
    ('Paranormal',     ('paranormal', 'unexplained')),
    ('Sci-Fi',         ('sci-fi', 'scifi', 'sci fi', 'science fiction', 'supernatural', 'fantasy')),
    ('Horror',         ('horror', 'scary')),
    ('Comedy',         ('comedy', 'komedi', 'humor', 'sitcom', 'stand-up')),
    ('True Crime',     ('true crime', 'real crime', 'forensic', 'detective', 'cold case', 'investigat')),
    ('Crime',          ('crime', 'mystery', 'thriller')),
    ('News',           ('news', 'business', 'finance', 'weather')),
    ('Movies',         ('movie', 'film', 'cinema')),
    ('Toons',          ('cartoon', 'toons', 'animation')),
    ('Kids',           ('kid', 'family')),
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
    ('Kung Fu Flicks', ('kung fu', 'martial art', 'kickboxing')),
    ('Sports',         ('sport', 'deportes', 'nfl', 'nba ', 'nhl', 'mlb', 'nascar')),
    ('Outdoors',       ('outdoor', 'fishing', 'hunting')),
    ('Music',          ('iheart', 'vevo', 'stingray', 'mtv ', 'concerts')),
    ('Ambiance',       ('ambient', 'fireplace', 'yule log', 'relaxation', 'lofi', 'slow tv')),
    ('Local News',     ('local news', 'fox local')),
    ('News',           ('news', 'noticias', 'weather', 'cnn', 'usa today', 'bloomberg')),
    ('True Crime',     ('true crime', 'court tv', 'cops', 'law & crime', 'forensic', 'dateline',
                        'first 48', 'cold case', 'investigat', 'detective', 'pluto tv crime')),
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
    ('Toons',          ('cartoon', 'toonami', 'toon goggles', 'looney tunes')),
    ('Kids',           ('kids', 'family', 'children', 'junior')),
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
