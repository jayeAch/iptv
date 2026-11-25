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
    2. NAME_PINS        whole-word channel/show names (alphabetical by category; precedence is PIN_ORDER)
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
#   Action     = classic action-adventure TV series (Walker Texas Ranger, MacGyver, Renegade,
#                Knight Rider); pinned above Crime so they win over it. Action MOVIE channels
#                stay in Movies; generic 'action' source groups stay in Entertainment
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
#   Latino     = Spanish-language channels (any genre: novelas, news, movies, ViX, Telemundo)
#   Asian      = K-drama, Bollywood, C-drama/TVB, Asian cinema and Asian-focused channels
#                (anime stays in Anime & Gaming, martial arts in Kung Fu Flicks)
#   Talk Shows = tabloid/daytime talk (Springer, Maury, Wilkos, Geraldo, Nosey, Dr. Phil)
#   Automotive = car culture: build/restoration shows, auctions, car-review shows
#                (Top Gear, Mecum, PetrolHeads); live racing stays in Sports
#   Soap Operas = daytime and primetime soaps (General Hospital, Days of our Lives, Y&R,
#                B&B, All My Children, Dynasty, Falcon Crest, Knots Landing, Melrose Place,
#                Peyton Place, Primetime Soaps); pinned above Classic TV and Drama. Spanish
#                novelas stay in Latino
#   Classic TV = vintage live-action network TV: sitcoms, variety, family and adventure
#                series (I Love Lucy, Andy Griffith, Happy Days, Carol Burnett, Love Boat) and
#                the Classic TV / Retro channels; pinned above Comedy/Crime/Drama/Toons so
#                they win regardless of the source group. Westerns stay in Westerns
#   Westerns   = western TV series and movie channels (Bonanza, Gunsmoke, Wild West,
#                Dr. Quinn); pinned by name (see NAME_PINS) so it beats Classic TV/Drama
#   Black Entertainment = channels marketed to Black audiences (BET, TV One,
#                Ebony TV, Bounce, Black cinema, Nollywood/African channels); pinned
#                by channel name (see NAME_PINS) so it beats Comedy/Crime/Movies
#   Toons      = cartoons / animated series (incl. adult animation); sits above
#                Comedy and Kids so animated comedies land here
#   Kids       = children's/preschool channels and shows (Nick Jr, PBS Kids, Baby Shark,
#                Cocomelon, Kidoodle); pinned AFTER Toons so cartoon networks stay Toons
#   Anime & Gaming = anime channels (Crunchyroll, RetroCrush, HIDIVE) and video-game
#                channels/franchises (Sensical Gaming, Sonic, Super Mario); pinned above
#                Toons and Sci-Fi so anime never lands there
#   Military   = military channels, war movies and WWII content (Military Heroes, Combat War
#                Channel, military history, WWII); pinned above History/Crime/Drama so they
#                win. Hogan's Heroes stays Classic TV
#   History    = history channels and shows (History Hit, History Unsealed); pinned above
#                Crime/Drama so they win. Military history moved to Military
#   Movies     = movie channels: any channel with movie/movies/films/cinema in the name; the
#                LAST name pin, so genre pins (Comedy, Crime, Horror, Kids, Christmas...) and
#                Classic TV/Westerns win first (Pluto TV Comedy Movies stays Comedy)
#   Documentaries = documentary channels/groups in any language incl. Scandinavian
#                (dokumentar, dokumentarer, dokumentär, dokumentärer, dokumentarfilm,
#                Finnish dokumentti); History, Nature, True Crime etc. stay separate
#   Food & Travel = food and travel channels (Pluto TV Food, Emeril Lagasse, Tastemade, Travelxp,
#                RVTV, 30A TV, Drone TV); pinned below Reality TV (Hell's Kitchen stays there)
#   Shopping   = home-shopping channels (QVC, HSN, ShopLC, Jewelry Television); no longer
#                part of Lifestyle
#   Game Shows = game/quiz shows and game-show channels (GSN, Buzzr, Family Feud,
#                Jeopardy); pinned above Reality TV, which no longer holds them
#   Christmas  = holiday/Christmas channels and holiday movie feeds; pinned first
#                (after Latino) so they beat Movies/Drama/Romance
#   LGB        = LGBTQ+ channels (OUTflix, Pluto TV Pride, Here TV, Dekkoo)
#   Women's Interests = channels and shows aimed squarely at a female audience (Lifetime Movies,
#                Dance Moms, Real Housewives, Bad Girls Club, Teen Mom, wedding/dress shows,
#                We TV reality, Rachael Ray); pinned above Reality TV, Romance and Entertainment so
#                they win. Holiday Lifetime feeds stay Christmas, Oxygen true crime stays True
#                Crime, women's sports stays Sports
#   Reality TV = unscripted reality / competition / dating / game shows
#                (Jersey Shore, Hell's Kitchen, Storage Wars, Deal or No Deal)
#   Romance    = romance movie channels and romance-driven series (Harlequin,
#                Hallmark Movies & More, Romance 365); holiday romance -> Christmas
#   Supernatural Drama = FICTIONAL supernatural series (Charmed, Ghost Whisperer,
#                Teen Wolf, Touched by an Angel); real-event shows stay in Paranormal
#   Home & DIY = home improvement, renovation, real estate, gardening, crafts, antiques and
#                how-to channels (Property Brothers, HGTV, Epic Gardening, 5-Minute Crafts,
#                Antiques Roadshow, Bob Ross); pinned above Reality TV and Food & Travel
#   Creators   = YouTube/TikTok-style creator channels (MrBeast, Smosh, Hot Ones, Mark Rober);
#                also every source group named 'Creators'; pinned above Reality TV
#   Pets & Animals = pet and animal-care channels (Dog Whisperer, Love Pets, Dr. Pol,
#                The Pet Collective); split out of Nature, pinned above it
#   Business   = business/finance news and market channels (Bloomberg, CNBC, Fox Business, Yahoo
#                Finance, Cheddar, Forbes, WSJ, MarketWatch); pinned just above News so they win
#                over it. The 'business'/'finance' source groups map here too
#   News       = news brands and network newsmagazines (CNN, Dateline, 20/20, 60 Minutes);
#                pinned below Latino/Sports. Local stations are NOT pinned: they come from the
#                'Local News'/'News' source groups and the name keywords below
#   Pop Culture = celebrity / entertainment-news channels (TMZ, Entertainment Tonight,
#                Inside Edition, BuzzFeed Celeb); pinned above Reality TV
#   Reality Competition = competition-format reality (Survivor, Amazing Race, Masked Singer,
#                Forged in Fire, Great British Baking); pinned above Reality TV, below Sports
#                so American Ninja Warrior stays Sports
#   Science    = science, space and how-things-work channels (Cosmos, MythBusters, Popular
#                Science, NASA, StarTalk); pinned above Nature/History/Documentaries
#   British TV = British-branded drama, mystery, comedy and classic series (BritBox, Acorn TV,
#                MHz, BBC Drama/Comedy, Midsomer Murders, Doctor Who); pinned above Comedy,
#                Crime and Drama. British home/baking/antiques shows stay in their topic category
# ---------------------------------------------------------------------------
CANONICAL_CATEGORIES: tuple[str, ...] = (
    'Action',
    'Ambiance',
    'Anime & Gaming',
    'Asian',
    'Automotive',
    'Black Entertainment',
    'British TV',
    'Broadcast',
    'Business',
    'Christmas',
    'Classic TV',
    'Combat Sports',
    'Comedy',
    'Creators',
    'Crime',
    'Documentaries',
    'Drama',
    'Entertainment',
    'Faith',
    'Food & Travel',
    'Game Shows',
    'History',
    'Home & DIY',
    'Horror',
    'International',
    'Kids',
    'Kung Fu Flicks',
    'Latino',
    'LGB',
    'Lifestyle',
    'Local News',
    'Military',
    'Movies',
    'Music',
    'Nature',
    'News',
    'Outdoors',
    'Paranormal',
    'Pets & Animals',
    'Pop Culture',
    'Reality Competition',
    'Reality TV',
    'Romance',
    'Sci-Fi',
    'Science',
    'Shopping',
    'Soap Operas',
    'Sports',
    'Supernatural Drama',
    'Talk Shows',
    'Toons',
    'True Crime',
    'Westerns',
    "Women's Interests",
)

DEFAULT_CATEGORY = 'Entertainment'

# ---------------------------------------------------------------------------
# 1. NAME OVERRIDES -- exact (lowercased) channel name -> category
# ---------------------------------------------------------------------------
NAME_OVERRIDES: dict[str, str] = {
    'nypd blue': 'Crime',
    'the nanny & mr sheffield - a fine romance': 'Comedy',
    'girlfriends': 'Black Entertainment',
    'lone star': 'Westerns',
    'survivor': 'Reality Competition',  # exact only: 'Designated Survivor' is a drama
    'clic': 'Latino',          # WHALE, Spanish
    'garfield': 'Latino',      # Roku, Spanish dub
    'danger tv': 'Latino',     # Roku Spanish feed (DangerTV is English)
    'outlaw': 'Westerns',
    'the canyon': 'Westerns',      # Young Riders (Airy)
    'sister, sister': 'Black Entertainment',  # Pluto
    'love thy neighbor': 'Black Entertainment',  # Tyler Perry
    'et': 'Pop Culture',       # exact only: whole-word pin 'et' would hit foreign titles
    'how to': 'Home & DIY',    # exact only: 'How to Get Away with Murder' is a drama
    'the kids in the hall': 'Comedy',  # sketch show, not a kids channel
    'bbc comedy': 'Comedy',
    # Local stations whose city name matched a pin (Las Vegas -> Action, Nashville -> Drama, Santa -> Christmas)
    'fox 5 kvvu las vegas': 'Local News',
    'kvvu fox 5 las vegas nv': 'Local News',
    'ksnv nbc 3 news las vegas, nv': 'Local News',
    'ktnv 13 action news las vegas': 'Local News',
    'keyt abc news channel 3 santa barbara ca': 'Local News',
    'wtvf channel5 news nashville': 'Local News',
    'cnn noticias': 'Latino',         # Spanish-language; the 'cnn' News pin would take it
    'the fbi': 'Classic TV',          # 1965 series; 'The FBI Files' stays True Crime
    # --- Entertainment clean-up (Nov 2026): exact names found in the catch-all ---
    'rules of engagement': 'Comedy',
    'life with derek': 'Comedy',
    'murdoch mysteries': 'Crime',
    'the doctor blake mysteries': 'Crime',
    'hunter': 'Crime',
    'ion': 'Crime',
    'ion plus': 'Crime',
    'ion television': 'Crime',
    'lawless': 'Crime',
    'reelz famous and infamous': 'True Crime',
    'sea patrol': 'Action',
    'designated survivor': 'Drama',
    'degrassi': 'Drama',
    'stories by amc': 'Drama',
    'pulse': 'Drama',
    'cw forever': 'Classic TV',
    'cw gold': 'Classic TV',
    'peak tv': 'Classic TV',
    'best of times': 'Classic TV',
    'passtime tv': 'Classic TV',
    'shout! tv': 'Classic TV',
    'shattered tv': 'Horror',
    'rnr tv': 'Sci-Fi',
    'on patrol: live': 'Reality TV',
    'highway thru hell': 'Reality TV',
    'paternity court': 'Reality TV',
    'one of the girls - med kenny aleksandr': 'Reality TV',
    'portrait tv': 'Reality TV',
    'the biggest loser': 'Reality Competition',
    'the doctors': 'Talk Shows',
    'boligkøb i blinde': 'Home & DIY',
    'boligkjøp i blinde': 'Home & DIY',
    'buying blind denmark': 'Home & DIY',
    'skønne ombygninger': 'Home & DIY',
    'stugfixarna norge': 'Home & DIY',
    "i shouldn't be alive": 'Documentaries',
    'get.factual': 'Documentaries',
    'wonder': 'Documentaries',
    'shockwave': 'Documentaries',
    'river monsters': 'Nature',
    'real wild': 'Nature',
    'magellantv wildest': 'Nature',
    'robot wars by mech+': 'Science',
    'wheels & steel': 'Automotive',
    '5 trucking hell': 'Automotive',
    'electricnow': 'Entertainment',  # Electric Entertainment shows (The Librarians, Leverage), not music
    'lionsgate network': 'Movies',
    'tribeca': 'Movies',
    'paramount+ picks': 'Movies',
    'pure flix tv': 'Faith',
    'byutv': 'Faith',
    'people are awesome': 'Pop Culture',
    'ideas en 5 minutos': 'Latino',
    'al loba': 'Latino',
    'cindie tv': 'Latino',
    'feva tv': 'Black Entertainment',
    'cna': 'News',
    'tv rain': 'International',
    'merhaba amour': 'International',
    'arous beiru': 'International',
    'stiletto': 'International',
    'til julefrokost hos r8dio': 'Christmas',
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


# Brackets/parentheses/braces, colons and hyphens/dashes. Treated as spaces so '[MLS] A vs B',
# '[Ligue-1] ...' and 'Chapecoense-SC' all match plain-word pins ('mls', 'ligue 1').
_BRACKETS_HYPHENS = re.compile(r'[\[\](){}:\-\u2010-\u2015]+')


def _squash(text: str) -> str:
    """Replace brackets, colons and hyphens with spaces and collapse whitespace."""
    return ' '.join(_BRACKETS_HYPHENS.sub(' ', text or '').split())


def _norm(text: str) -> str:
    """Lowercase, strip accents, apostrophes, brackets and hyphens for pin matching."""
    return _squash(_APOSTROPHES.sub('', _fold_accents(text or '').lower()))


def _words(block: str) -> tuple[str, ...]:
    return tuple(w.strip() for w in block.strip().splitlines() if w.strip())


NAME_PINS: dict[str, tuple[str, ...]] = {
    # Alphabetical by category so blocks are easy to find. Matching PRECEDENCE is NOT
    # the order here: it is PIN_ORDER below. When you add a block, add its key to
    # PIN_ORDER too (an assert checks both lists agree).
    # Classic action-adventure TV series. Sits above Crime in PIN_ORDER so Walker Texas
    # Ranger (formerly a Crime pin) and similar shows land here. Action movie channels
    # ('Pluto TV Action Movies') are not listed: they stay in Movies.
    'Action': _words("""
        walker texas ranger
        the outpost
        the librarians
        pluto tv retro action
        hercules
        pluto tv rocky + creed
        warrior princess
        xena
        complex tv
        mgm presents: action
        filmrise action
        pluto tv action
        las vegas
        nikita
        universal action
        all action
        action plus
        pluto tv 80's action
        pluto tv more 80's action
        xumo free action
        macgyver
        renegade
    """),
    # Stingray's ambient/wellness video channels (its music channels stay in Music).
    'Ambiance': _words("""
        pluto tv peis
        pluto tv pejs
        pluto tv eldstad
        naturescape
        zenlife
        zen life
        stargaze
        cozy cafe
        cityscapes
        stingray spa
    """),
    # Anime and video games. Above Toons and Sci-Fi (Pluto TV Anime, Sonic, Super Mario
    # would otherwise land there). Estrella Games is Latino (pinned first).
    'Anime & Gaming': _words("""
        anime
        caylus
        bizaar
        avatar
        one piece
        crunchyroll
        retrocrush
        inazuma
        yu-gi-oh
        naruto
        sailor moon
        gaming
        game-on
        sonic the hedgehog
        super mario brothers
    """),
    # Asian content (K-drama, Bollywood, TVB, Asian cinema). Anime stays in Anime & Gaming.
    'Asian': _words("""
        amasian tv
        aliwko
        goldentv
        asiancrush
        discovering china
        asian drama
        bollywood
        cj enm k-stories
        genie k-movie
        jubao
        k-drama
        k.movies
        kmovies
        kfood
        korean
        rakuten viki
        series k edge
        series k heart
        series k legacy
        thailand tv
        joysauce
        tvb pearl drama
    """),
    # Car culture: build/restoration shows, auctions, car-review shows. Live racing
    # (F1, NASCAR, MotoGP, Racer, etc.) stays in Sports above. Sits before Outdoors.
    'Automotive': _words("""
        top gear
        petrolheads
        mecum
        classic car
        torque
        pimp my ride
        powernation
        hagerty
    """),
    # Channels marketed to Black audiences. Sits after Faith (so BET Gospel stays
    # Faith) and above Paranormal/True Crime/Crime/Comedy/Drama so TV One Crime &
    # Justice, BET Throwbacks etc. land here. Whole-word match: 'bet' catches every
    # "BET ..." channel; 'tv one' catches the TV One channels.
    'Black Entertainment': _words("""
        bet
        perform
        peekflick
        urban tv
        tv one
        allblk
        beyond the gates
        avaz
        black enterprise streaming television
        black family sitcoms
        blackpix
        bounce xl
        ebony tv
        filmrise black tv
        fox soul
        maverick black cinema
        mgm celebrates black cinema
        pam griers soul flix
        retro black laughs
        revolt mixtape
        shades of black
        urban action channel
        xumo free black cinema
        def jam
        wild n out
        its showtime at the apollo
        50 cent action
        wendy williams
        growing up hip hop
        braxton family values
        afrolandtv
        afroland crime
        afroland family
        afroland nollywood
        afroland romance
        afroland thriller
        nolly africa
        tidpix
        demand africa
    """),
    # British-branded drama/mystery/comedy/classic series. Above Comedy/Crime/Drama in PIN_ORDER. Home, baking and
    # antiques shows are NOT listed: they stay in Home & DIY / Reality Competition.
    'British TV': _words("""
        bbc drama
        bbc sci-fi
        bbc one
        stv
        bbc
        bbc adventure
        britbox
        acorn tv
        mhz mysteries
        filmrise british tv
        british screen classics
        midsomer murders
        mi-5
        new tricks
        a touch of frost
        doctor who
    """),
    # Business / finance news and market channels. Sits right above News in PIN_ORDER so
    # Bloomberg, CNBC, Fox Business, Cheddar etc. land here instead. Kept specific: no bare
    # 'money' (Money Heist), 'fortune' (Wheel of Fortune) or 'crypto'.
    'Business': _words("""
        bloomberg
        bnn bloomberg
        cnbc
        fox business
        yahoo finance
        the diary of a ceo
        cheddar
        forbes
        wsj
        wall street journal
        marketwatch
        barrons
        business insider
        thestreet
        tipranks
        benzinga
        kitco
        tradingview
        motley fool
        ramsey network
    """),
    # Christmas / holiday channels. Right after Latino so they beat Movies, Drama,
    # Romance and Faith ("Hallmark Movies & More Holiday Favorites" -> Christmas).
    'Christmas': _words("""
        christmas
        pluto tv jul
        jingle bells
        pluto tv de bedste julespecials
        pluto tv de beste julespesialene
        pluto tv julehygge
        pluto tv julekos
        pluto tv julmys
        pluto tv mistletoe
        andersens julehemmelighed
        holiday
        holidays
        yule
        santa
    """),
    # Classic TV: vintage live-action network shows and the Classic/Retro channels.
    # Above Comedy/Crime/Drama/Toons so e.g. I Love Lucy is not filed as Comedy.
    # Westerns, Black Entertainment, Sports etc. sit higher and still win. Add a show
    # here to force it into Classic TV (apostrophes are stripped: hogans heroes).
    'Classic TV': _words("""
        classic tv
        three's company
        tv land
        i love lucy
        dick van dyke
        andy griffith
        beverly hillbillies
        green acres
        the addams family
        the brady bunch
        happy days
        leave it to beaver
        hogans heroes
        lassie
        family affair
        that girl
        the carol burnett show
        the ed sullivan show
        johnny carson
        three stooges
        mission impossible
    """),
    # Combat sports first so these beat the Kung Fu Flicks and Sports pins below.
    'Combat Sports': _words("""
        mma
        ufc
        swerve combat
        fight network
        one championship
        glory kickboxing
    """),
    # Must sit ABOVE Crime: the Crime list has the whole word 'mystery', which
    # would otherwise capture "Mystery Science Theater 3000".
    'Comedy': _words("""
        mystery science theater
        mst3k
    """),
    'Comedy (after Classic TV)': _words("""
        comedy central
        according to jim
        pluto tv comedy
        8 out of 10 cats
        the red green channel
        mr bean live action
        komedi
        laff
        the daily show
        weeds & nurse jackie
        komedie
        the nanny
        rifftrax
        just for laughs
        comedy vault
        spin city
        dry bar comedy
        great comedy
        laff more
        sitcom
        sitcoms
        snl vault
    """),
    'Crime': _words("""
        cagney & lacey
        rookie blue
        third watch
        nash bridges
        the practice
        prime suspect
        crime thrillher
        pluto tv crime thrillers
        pluto tv crime drama
        pluto tv crime movies
        criminal minds
        csi
        jag
        diagnosis murder
        hawaii five-o
        in the heat of the night
        law & order
        matlock
        mystery
        ncis
        perry mason
        sherlock
    """),
    # Creator channels. Above Reality TV so they stop falling into the Entertainment catch-all. Unknown names
    # arrive via the 'creators' source group (RAW_GROUPS).
    'Creators': _words("""
        mrbeast
        fgteev
        w3
        ssundee
        filmrise creators
        wonderviz
        jesser
        zhc
        ben azelart
        simba prisen
        magnus midtbo
        sam and colby
        ryan trahan
        like nastya
        smosh
        hot ones
        mark rober
        dhar mann
        law by mike
        jesse james west
        creator tv
        brat tv
        the royalty family
        celine dept
        ishowspeed
        jubilee
    """),
    # Documentary channels, incl. Scandinavian spellings (accents are folded, so
    # dokumentär == dokumentar). History/Nature/True Crime sit in their own lists.
    'Documentaries': _words("""
        documentary
        icons
        docu vision
        discovery channel
        modern marvels
        reelz famous & infamous
        dokumentar
    """),
    # Non-crime dramas (crime dramas stay in Crime above).
    'Drama': _words("""
        more tv drama
        love boat
        legacy
        candyjar
        wildfire
        nashville
        hearties
        filmrise drama
        felicity
        nip/tuck
        pluto tv drama
        wild at heart
        vikings
        7th heaven
        beverly hills 90210
        dawsons creek
        heartland
        little house on the prairie
    """),
    # Religious / devotional channels. Whole-word matching, so keep entries
    # specific (a bare 'faith' or 'jesus' would hit Judge Faith / Black Jesus).
    'Faith': _words("""
        jewish life tv
        in touch +
        jltv
        real life network
        dove
        trinity broadcast network
        cowboy christian network
        daystar
        daystartv
        elevation church
        faithnfreedomtv
        faithope
        god tv
        impact gospel
        joel osteen network
        sikh ratnavali
    """),
    # Food and travel channels. Below Reality TV (Hell's Kitchen, Kitchen Nightmares stay
    # there) and above Paranormal/Documentaries/Comedy/Drama. Kept specific: no bare
    # 'travel' or 'kitchen' (Time Travel, Kitchen Cabinet). Pluto DK cooking/dinner shows
    # (Koch'en på toppen, Til Middag Hos, Med kniven for struben) are food too.
    'Food & Travel': _words("""
        pluto tv food
        awe plus
        no reservations
        destination america
        food network
        tastemade
        emeril lagasse
        nordic cookery
        sweet escapes
        kochen pa toppen
        til middag hos
        med kniven for struben
        journy
        rvtv
        30a tv
        drone tv
    """),
    # Game shows. Sits above Reality TV so these no longer land there. After Sports/
    # Outdoors so 'American Ninja Warrior' stays Sports.
    'Game Shows': _words("""
        pluto tv game shows
        funny you should ask
        buzzr
        deal or no deal
        family feud
        the chase
        tipping point
        pointless
        the price is right
        lets make a deal
        supermarket sweep
        celebrity name game
    """),
    # History. After True Crime and Paranormal (Ancient Aliens, Curse
    # of Oak Island stay Paranormal) and above Crime/Drama ("History's Mysteries" -> History).
    # 'Xtreme Outdoor by History' stays Outdoors (pinned earlier).
    'History': _words("""
        history
    """),
    # Home improvement, real estate, gardening, crafts, antiques, how-to. Above Reality TV and Food & Travel.
    'Home & DIY': _words("""
        home diy
        home refresh
        home crashers
        home.made.nation
        home. made. nation
        family handyman
        this old house
        places & spaces
        diy art
        flipping nation
        hgtv
        property brothers
        chip & jo
        million dollar listing
        million dollar dream home
        tiny house nation
        extreme makeover home edition
        epic gardening
        garden with monty don
        bbc home & garden
        craftsytv
        5 minute craft
        5-minute crafts
        antiques road trip
        antiques roadshow
        bargain hunt
        homes under hammer
        a new life in the sun
        escape to the country
        lx home
        martha stewart
        bob ross
    """),
    'Horror': _words("""
        dread tv
        fearstream
        frightflix
        alfred hitchcock presents
        horror
        tales from the crypt
        itn's creatureverse
        pluto tv terror
        scare o ween
        scares
        scream factory
        screambox
        watch it scream
    """),
    # Children's / preschool channels. LAST so Toons (Cartoon Network, Boomerang, Toon
    # Goggles) and Comedy win first. Keep 'family' out: Family Feud, Addams Family etc.
    # 'The Kids in the Hall' is a NAME_OVERRIDE (Comedy).
    'Kids': _words("""
        kids
        123 go
        kiddo
        mr bean animated
        pluto tv klassiske tegnefilmer
        nicktoons
        teenvee
        he-man & the masters of the universe
        playworld
        svampbob
        pluto tv tegnefilm
        pluto tv klassiske tegnefilm
        det stora experimentet
        det store eksperiment
        det store eksperimentet
        familien thunderman
        familjen thunderman
        svampebob
        kidz
        pluto tv tecknade
        pluto tv tecknat
        totally turtles
        pluto tv ungdomsserier
        icarly
        smurfarna
        smurfene
        smølferne
        junior
        baby shark
        henry danger
        moonbug
        ryan and friends
        nick jr
        nickelodeon
        sesame street
        paw patrol
        peppa pig
        blippi
        caillou
        barney
        teletubbies
        wiggles
        strawberry shortcake
        my little pony
        power rangers
        kartoon channel
        kidsflix
        super simple
        mister rogers
    """),
    # Martial arts next so MMA/UFC/kickboxing beat the generic Sports pins below.
    'Kung Fu Flicks': _words("""
        kung fu
        flicks of fury
        hi-yah!
        wu tang collection
        hong kong fight club
    """),
    # Spanish-language channels, any genre. FIRST so "CSI en Espanol", "ViX Deportes",
    # "Telemundo Noticias" etc. beat the English-name pins below (csi, cops, sports...).
    'Latino': _words("""
        spanish
        123go! español
        gritos
        univision
        galavisión
        en espanol
        tock tock movies
        conecta
        zona investigación
        telemundo
        vix
        4fangs
        canela.tv
        novelas
        runtime español
        novelisima
        freetv
        cine de oro
        cine de horror
        cine retro
        cine romantico
        aqui y ahora
        azteca internacional
        caracol mix
        caso cerrado
        como dice el dicho
        crimenes verdaderos
        cuando los angeles caen
        delito
        el senor de los cielos
        emocion atres tv
        estrella games
        autopista al cielo
        peliculas gratis
        series gratis
        galanes
        grandes parejas
        jajaja
        jorge ramos
        las 3 marias
        latinus
        los asesinatos de midsomer
        mi miedo canal
        mi raza canal
        misterios sin resolver
        pelimex
        pequenos gigantes
        pitufo tv
        rebelde
        runtime sangre fria
        telediario now
        todo familia
        tv amor
        tvoai channel
        vas o no vas usa
        venevision internacional
        unica tv
    """),
    # LGBTQ+ channels. Keep 'pride' specific: a bare 'pride' would hit Pride & Prejudice.
    'LGB': _words("""
        the l word
        logo pluto tv
        outflix
        pluto tv pride
        revry
    """),
    # Military: above History so military-history channels land here. Whole-word match;
    # keep entries specific ('heroes' alone would catch Hogan's Heroes).
    # Self-improvement, wellness, fashion/beauty and general lifestyle channels. Sits after
    # Food & Travel and Reality TV in PIN_ORDER so cooking/travel and reality channels keep
    # their own categories. Tony Robbins/GrowthDay arrive under a "Game Shows" source group.
    'Lifestyle': _words("""
        tony robbins network
        growthday network
        4uv
        fashiontv
    """),
    'Military': _words("""
        military
        war channel
    """),
    # Movie channels. LAST pin: Comedy/Crime/Horror/Sci-Fi/Kids/Christmas/Romance/Westerns
    # etc. win first, so 'Pluto TV Comedy Movies' stays Comedy. Catches movie channels that
    # arrive under a generic group (Entertainment, Sci-Fi) like 'FilmRise Free Movies'.
    'Movies': _words("""
        movie
        movies
        film
        films
        cinevault
        moviesphere
        cinema
    """),
    # Nature / wildlife / pets. ABOVE History: Samsung files these under its "History"
    # group. (Nature is in category_exclude_list.py, so these are dropped from output.)
    # Music-only channels (Stingray, VEVO, iHeart, Pluto/MTV music feeds, concerts, karaoke).
    # Sits after Ambiance/Christmas/Latino/Black Entertainment in PIN_ORDER so Stingray's
    # ambient channels, holiday music, Spanish music and BET-style music channels land there
    # first; bare 'stingray' therefore only catches what is left (its music channels).
    # Genre phrases are Pluto-prefixed on purpose: bare 'hip hop' would hit 'Hip Hop Squares'.
    'Music': _words("""
        stingray
        vevo
        iheart
        qello
        loupe
        mtv biggest pop
        mtv classic
        live music
        thats rock
        thats 80s
        thats 90s00s
        billboard
    """),
    'Nature': _words("""
        pbs nature
        terra mater wild
        adventure earth
        nat geo
        go wild
        brave wilderness
        dinos 24/7
        animal planet
        earthday 365
        bbc earth
        wildearth
        weatherspy
        samsung wild life
        love nature
    """),
    # News brands and network newsmagazines (Dateline, 20/20, 60 Minutes). Sits right after Sports
    # so Latino (Dateline en Español, CNN Noticias) and Sports ('60 Minutes Sports') win first and
    # newsmagazines no longer land in True Crime/History. Deliberately NO bare 'news' pin: it would
    # drag the 'Local News' source group into News. Local stations stay with the group/keyword rules.
    'News': _words("""
        dateline
        reuters 60
        dateline nbc
        20/20
        ms now
        fox livenow
        60 minutes
        real america's voice
        nightline
        face the nation
        meet the press
        cbs sunday morning
        good morning america
        world news tonight
        nbc nightly news
        cbs evening news
        cnn
        msnbc
        newsmax
        newsnation
        usa today
    """),
    'Outdoors': _words("""
        outdoor
        all-out alaska
        outdoors
        outside
        pursuitup
        wired2fish
        game & fish tv
        hunt fish tv
        fishing
        bassmaster
        life below zero
        inwonder
    """),
    'Paranormal': _words("""
        pluto tv overnaturlig
        a haunting
        alien files
        unidentified
        unxplained zone
        pluto tv alien invasion
        alien nation
        ancient aliens
        mysteries xplored
        fear zone
        ghost dimension
        ghost hunters
        ghost stories
        haunt tv
        haunttv
        i was haunted
        monsters are real
        most haunted
        paranormal
        ghosts are real
        mysterious worlds
        the dead files
        the unxplained with william shatner
    """),
    # Pet and animal-care channels, split out of Nature (which sits below this). Above Reality TV.
    'Pets & Animals': _words("""
        dog whisperer
        cesars pack leader tv
        love pets
        paws & claws
        pet collective
        rovr pets
        dr pol
        the yorkshire vet
        lucky dog
        horse & country
        dogtv
        jack hanna
    """),
    # Celebrity / entertainment-news channels. Above Reality TV.
    'Pop Culture': _words("""
        tmz
        entertainment tonight
        inside edition
        buzzfeed celeb
        hollywire
    """),
    # Competition-format reality. Above Reality TV; below Sports so 'American Ninja Warrior' stays Sports.
    # 'survivor' is an exact NAME_OVERRIDE ('Designated Survivor' is a drama).
    'Reality Competition': _words("""
        the amazing race
        fear factor
        wipeout
        mtv ink master
        married to medicine
        mtv pluto tv
        big brother
        the challenge
        got talent
        masked singer
        fool us
        masterchef
        top chef
        project runway
        hells kitchen
        iron chef
        forged in fire
        alone by history
        great british baking channel
        great british menu
    """),
    # Unscripted reality / competition / dating. (Game shows have their own category.) After Black Entertainment
    # (Growing Up Hip Hop etc. stay there) and Automotive/Outdoors/Sports (Fast N' Loud,
    # Life Below Zero, American Ninja Warrior). Bare 'survivor' is a NAME_OVERRIDE.
    'Reality TV': _words("""
        reality
        nathan for you
        the curse of oak island
        emergency rescue
        a&e alaska state troopers
        the therapy crouch
        afterwork tv
        paradise
        the osbournes
        ice road truckers
        dangertv
        untold stories of the
        chaos on cam
        earthxtra
        supernanny
        grind hard plumbing co
        extreme living
        pluto tv urbex
        car chase
        extreme jobs
        wicked tuna
        deal masters
        dog the bounty hunter
        deal zone
        judge judy
        hot bench
        bravo vault
        e! keeping up
        the hills
        jersey shore
        geordie shore
        ex on the beach
        love island
        temptation island
        dating naked
        catfish
        cheaters
        intervention
        hoarders
        gordon ramsay
        kitchen nightmares
        bar rescue
        pluto tv tough jobs
        come dine with me
        shark tank
        bondi rescue
        storage wars
        auction hunters
        pawn stars
        american pickers
        swamp people
        ax men
        ice pilots
        duck dynasty
    """),
    # Romance movie channels and romance-driven series. After Christmas (holiday romance
    # -> Christmas) and Latino (Cine Romantico), above Drama/Movies.
    'Romance': _words("""
        romance
        pluto tv romantik
        pluto tv romantikk
        great american romcoms
        harlequin
        light & love
        love & drama
        hallmark movies & more
    """),
    # Sci-Fi above Horror: anything both is Sci-Fi. These sit above the raw
    # group lookup so channels sourced under a generic "Movies" group still
    # land in their genre.
    'Sci-Fi': _words("""
        the asylum
        comet
        the twilight zone
        dark matters
        mutant x
        unidentified
        andromeda
        beyond belief
        farscape
        outersphere
        dark matter tv
        fantasy & horror
        pluto tv fantastic
        sci-fi
        scifi
        stargate
        star trek
    """),
    # Science / space / how-things-work. Above Nature, History and Documentaries. No bare 'science' pin (it would
    # hit 'Mystery Science Theater 3000' and 'science fiction').
    'Science': _words("""
        science is amazing
        popular science
        startalk
        cosmic frontiers
        curiosity now
        mythbusters
        space & beyond
        space series
        space live
    """),
    # Home-shopping channels. Whole-word match, so keep entries specific.
    'Shopping': _words("""
        ontv4u
        qvc
        qvc2
        hsn
        shoplc
        shop lc
    """),
    # Soap operas. Above Classic TV and Drama (both used to hold them). Whole-word
    # match; ambiguous one-word titles (Passions, Loving, Santa Barbara) are in
    # NAME_OVERRIDES. 'dynasty' is safe: Duck Dynasty is pinned to Reality TV above.
    'Soap Operas': _words("""
        soaps
        eastenders
        all my children
        general hospital
        the bold and the beautiful
        dynasty
        melrose place
    """),
    'Sports': _words("""
        sport
        darts
        mex 1
        axs
        sec network 
        yes network 
        acc
        ligue-1
        mls
        sports
        big ten network
        adr+
        ncaa
        tsn
        cfb
        eredivisie
        efl
        wild tv
        rightnow
        great by choice
        espn
        espnu
        fanduel racing
        espn2
        dazn
        introuble
        nfl
        nba
        mvmt
        lacrosse
        mlb
        nhl
        nascar
        nhra
        wwe
        aew
        pga tour
        golf
        golfpass
        dp world tour
        acc digital network
        accdn
        big 12 studios
        pac-12 insider
        nesn
        msg
        overtime
        stadium
        tudn
        itv deportes
        uefa.tv
        fifa
        real madrid
        barca 24h
        top barca
        lo mejor de liga
        f1 channel
        motogp channel
        racer select
        racing america
        rally tv
        fuel tv
        monster jam
        flohockey
        floracing 24/7
        pickleballtv
        pickletv
        billiard
        billiards
        bowling tv
        snooker 900
        tennis
        strongman
        surfer tv
        surf cinema
        world surf league
        top rank classics
        hard knocks
        the jim rome show
        pluto tv handboll
        soccer
        cricket
        rugby
        wrestling
        boxing
        pokergo
        poker night
        triton poker
        world poker tour
        esports edge
        team liquid
        american ninja warrior
        american gladiators
    """),
    # Fictional supernatural series. After Paranormal ('Long Island Medium', 'Ghost Hunters'
    # stay there) and above Sci-Fi/Drama/Comedy. Bare 'medium'/'angel' are NAME_OVERRIDES.
    'Supernatural Drama': _words("""
        supernatural
        highway to heaven
        from dusk till dawn: the series
        charmed
        ghost whisperer
        touched by an angel
        sabrina the teenage witch
        van helsing
        teen wolf
        beauty and the beast
        the 4400
        medium
    """),
    # Tabloid / daytime talk. Judge shows and late night are not included.
    'Talk Shows': _words("""
        drew barrymore show
        graham norton
        trevor noah
        nosey
        dr. phil
    """),
    # Toons above Comedy/Kids: animated shows (incl. adult animation) stay here.
    'Toons': _words("""
        aqua teen hunger force
        pluto tv animation
        boomerang
        animation+
        beavis and butt-head
        comedy central animation
        metalocalypse
        mr. pickles
        primal
        adultswim
        adult swim
        rick and morty
        robot chicken
        samurai jack
        south park
        spongebob
        squidbillies
        the venture bros.
        toon goggles
        toons
    """),
    'True Crime': _words("""
        the fbi files
        chasing criminals
        american crimes
        crime 360
        law&crime
        real crime
        jail
        60 days in by a&e
        witness to justice
        medical examiner
        live channel
        filmrise mysteries
        i survived
        mysteria
        dark side of
        trublu
        cold case files
        cops
        48 hours
        court tv
        crime 24/7
        crimes cults killers
        crime cults killers
        crime files channel
        crime scenes
        crime up close
        crime zone
        first 48
        forensic
        i (almost) got away with it
        killer mysteries
        law & crime
        live pd
        locked up abroad
        love kills
        the new detectives
        to catch a smuggler
        true crime
        unsolved mysteries
        buzzfeed unsolved
        world's most evil killers
        world's wildest police videos
        xumo free crime tv
    """),
    # Westerns. Sits above Drama (Dr. Quinn) and Classic TV. Whole-word match:
    # 'western' catches Western TV, Western Bound, FilmRise Western, a-z Western
    # Grit, etc. Generic names (Lone Star, Outlaw, The Canyon) are in NAME_OVERRIDES.
    'Westerns': _words("""
        western
        grit xtra
        circle
        insp
        westerns
        wild west
        bonanza
        death valley days
        dr. quinn
        gunsmoke
        grjngo
        life & legend of wyatt earp
        rawhide
        the lone ranger
        the rifleman
        the virginian
        wanted: dead or alive
    """),
    # Channels/shows aimed squarely at women (Lifetime, We TV, Bravo/Oxygen reality,
    # wedding and dating-drama franchises). Above Reality TV/Romance/Entertainment in
    # PIN_ORDER. Lifetime holiday feeds stay Christmas (pinned earlier), Oxygen true
    # crime stays True Crime, Women's Sports Network / FIFA+ Women stay Sports.
    "Women's Interests": _words("""
        lifetime movies
        drop dead diva
        xumo her movies
        little women: la
        dance moms
        matched married meet
        hersphere
        real housewives
        bad girls club
        teen mom
        all weddings we tv
        all reality we tv
        love after lockup
        say yes to the dress
        rachael ray presents
        thesorrygirls tv
    """),
}

# Precedence: first matching key wins. Edit this order (not the dict above) to change which
# category beats which. Paranormal stays on top of True Crime; True Crime stays above Crime.
PIN_ORDER: list[str] = [
    'Latino',
    'Christmas',
    'LGB',
    'Asian',
    'Talk Shows',
    'Combat Sports',
    'Kung Fu Flicks',
    'Sports',
    'Business',
    'News',
    'Automotive',
    'Outdoors',
    'Ambiance',
    'Faith',
    'Black Entertainment',
    'Music',
    'Westerns',
    'Shopping',
    'Game Shows',
    'Anime & Gaming',
    'Creators',
    'Pets & Animals',
    'Home & DIY',
    'Pop Culture',
    "Women's Interests",
    'Reality Competition',
    'Reality TV',
    'Food & Travel',
    'Lifestyle',
    'Romance',
    'Paranormal',
    'Supernatural Drama',
    'True Crime',
    'Science',
    'Nature',
    'Military',
    'History',
    'Documentaries',
    'British TV',
    'Comedy',
    'Action',
    'Crime',
    'Drama',
    'Sci-Fi',
    'Horror',
    'Toons',
    'Soap Operas',
    'Classic TV',
    'Comedy (after Classic TV)',
    'Kids',
    'Movies',
]
assert sorted(PIN_ORDER) == sorted(NAME_PINS), \
    f'NAME_PINS / PIN_ORDER mismatch: {set(PIN_ORDER) ^ set(NAME_PINS)}'


def _compile_pins() -> list[tuple[str, re.Pattern]]:
    out = []
    for key in PIN_ORDER:
        label, words = key.split(' (')[0], NAME_PINS[key]
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
        'mood + ambiance',
    ),
    'British TV': (
        'british drama', 'british comedy',
    ),
    'Creators': (
        'youtube creators', 'creator',
    ),
    'Home & DIY': (
        'home', 'home & design', 'diy', 'home improvement',
    ),
    'Pets & Animals': (
        'pets', 'pets + animals',
    ),
    'Pop Culture': (
        'celebrity', 'celebrity news', 'entertainment news',
    ),
    'Reality Competition': (
        'competition shows',
    ),
    'Science': (
        'science & technology', 'science + tech', 'space', 'space & science',
    ),
    'Anime & Gaming': (
        'anime', 'anime & animation', 'animation & anime', 'gaming', 'gaming & anime', 'games',
        'video games',
    ),
    'Asian': (
        'asian drama', 'bollywood', 'k-drama', 'k-movie', 'korean',
    ),
    'Automotive': (
        'auto & motor',
    ),
    'Business': (
        'business', 'business & finance', 'business news', 'finance', 'financial news',
        'markets', 'money',
    ),
    'Christmas': (
        'christmas movies', 'holiday', 'holidays', 'holiday movies', 'xmas',
    ),
    'Classic TV': (
        'classic', 'classic tv & movies', 'classics', 'nostalgia', 'retro', 'vintage tv',
        'westerns + classics',
    ),
    'Comedy': (
        'comedia', 'humor', 'komedi', 'komedie', 'sitcoms',
    ),
    'Crime': (
        'crime + thriller', 'crime drama', 'crime dramas', 'crime tv', 'crimen', 'mysteries',
        'mystery', 'mystery & suspense', 'police dramas',
    ),
    'Documentaries': (
        'biographies', 'biography', 'docs', 'documentaires', 'documentales', 'documentari',
        'documentary', 'documentary & biography', 'dokumentar', 'dokumentarer',
        'dokumentarfilm', 'dokumentarfilmer', 'dokumentti', 'dokumentit', 'dokumenttielokuvat',
        'dokus & wissen', 'education', 'educational', 'factual', 'history + docs',
        'infotainment', 'special interest', 'true stories',
    ),
    'Drama': (
        'drama series', 'dramas', 'serie', 'serier', 'series', 'tv dramas',
    ),
    'Entertainment': (
        'action', 'action & adventure', 'action & drama', 'action and adventure',
        'action/adventure', 'adventure', 'bingeworthy', 'divertissement', 'entretenimiento',
        'featured', 'general', 'intrattenimento', 'new on pluto tv', 'real life adventure',
        'tv', 'tv & entertainment', 'tv & movies', 'tv shows',
    ),
    'Faith': (
        'devotional', 'inspiration + faith',
    ),
    'Food & Travel': (
        'cucina & viaggi', 'food', 'food + travel', 'travel', 'travel & lifestyle',
    ),
    'Game Shows': (
        'game show', 'gameshows',
    ),
    'History': (
        'histoire', 'historia', 'storia', 'geschichte',
    ),
    'Horror': (
        'scary',
    ),
    'Kids': (
        'bambini', 'children', 'childrens', 'jeunesse', 'kids & family', 'kids + family',
        'kids and family', 'niños',
    ),
    'Kung Fu Flicks': (
        'kung fu', 'martial arts',
    ),
    'Latino': (
        'en español',
    ),
    'LGB': (
        'lgbt', 'lgbtq', 'lgbtq+', 'lgbtqia', 'lgbtq+ & pride', 'pride',
    ),
    'Lifestyle': (
        'culture + lifestyle', 'home & food',
    ),
    'Local News': (
        'local channels', 'regional news',
    ),
    'Military': (
        'military & war', 'war', 'war movies',
    ),
    'Movies': (
        'cine', 'cinéma', 'film', 'filme', 'filmer', 'movie channels',
    ),
    'Music': (
        'musica', 'musik', 'musique',
    ),
    'Nature': (
        'nature + science',
    ),
    'News': (
        'actualités', 'news + opinion', 'noticias', 'nyheter',
    ),
    'Outdoors': (
        'outdoor',
    ),
    'Paranormal': (
        'ghost hunting', 'ghosts', 'paranormal & unexplained', 'paranormal + unexplained',
        'paranormal tv', 'ufo', 'ufos', 'ufos & aliens', 'unexplained',
    ),
    'Reality TV': (
        'daytime tv & games', 'dating', 'reality', 'reality shows',
    ),
    'Romance': (
        'romance movies', 'romantic', 'romantic comedy',
    ),
    'Sci-Fi': (
        'adrenaline & sci-fi', 'fantasy', 'horror & sci-fi', 'sci fi', 'scifi',
        'sci-fi & fantasy', 'sci-fi & horror', 'sci-fi & supernatural', 'sci-fi + fantasy',
        'sci-fi and horror', 'science fiction',
    ),
    'Soap Operas': (
        'daytime soaps', 'primetime soaps', 'soap opera', 'soaps',
    ),
    'Supernatural Drama': (
        'supernatural',
    ),
    'Toons': (
        'animated', 'animation', 'cartoon', 'cartoons',
    ),
    'Shopping': (
        'home shopping', 'shop', 'teleshopping', 'tv shopping',
    ),
    'Sports': (
        'calcio', 'deporte', 'motor sports', 'sport', 'sports & outdoors', 'sports on now',
    ),
    'Westerns': (
        'western',
    ),
    "Women's Interests": (
        'women', "women's", 'womens', "women's interest", 'womens interest',
        'womens interests', 'women lifestyle',
    ),
    'True Crime': (
        'cold cases', 'detective', 'forensics', 'investigation', 'investigations',
        'real crime', 'true crime tv',
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
#   - Local News precedes News; Business precedes News ('business news' -> Business).
#   - Paranormal precedes Supernatural Drama, which precedes Sci-Fi.
#   - Christmas precedes Romance/Movies (holiday movies are Christmas).
#   - Romance precedes Comedy/Movies/Drama (romantic comedy is Romance).
#   - Sci-Fi precedes Horror (so anything both is Sci-Fi).
#   - True Crime precedes Crime.
#   - Game Shows precedes Reality TV; History precedes Documentaries; Shopping precedes Lifestyle.
# ---------------------------------------------------------------------------
RAW_KEYWORD_RULES: list[tuple[str, tuple[str, ...]]] = [
    ('Combat Sports',  ('combat sport',)),
    ('Kung Fu Flicks', ('martial art', 'kung fu')),
    ('Christmas',      ('christmas', 'xmas', 'holiday')),
    ('LGB',            ('lgb', 'queer')),
    ('Outdoors',       ('outdoor',)),
    ('Asian',          ('asian', 'korean', 'k-drama', 'bollywood')),
    ('Local News',     ('local', 'regional')),
    ('Paranormal',     ('paranormal', 'unexplained')),
    ('Supernatural Drama', ('supernatural',)),
    ('Sci-Fi',         ('sci-fi', 'scifi', 'sci fi', 'science fiction', 'fantasy')),
    ('Horror',         ('horror', 'scary')),
    ('Romance',        ('romance', 'romantic')),
    ('Comedy',         ('comedy', 'komedi', 'humor', 'sitcom', 'stand-up')),
    ('True Crime',     ('true crime', 'real crime', 'forensic', 'detective', 'cold case', 'investigat')),
    ('Crime',          ('crime', 'mystery', 'thriller')),
    ('Business',       ('business', 'finance', 'financial')),
    ('News',           ('news', 'weather')),
    ('Movies',         ('movie', 'film', 'cinema')),
    ('Toons',          ('cartoon', 'toons', 'animation')),
    ('Kids',           ('kid', 'family')),
    ('Sports',         ('sport', 'soccer', 'football', 'basketball')),
    ('Music',          ('music', 'radio')),
    ('Game Shows',     ('game show', 'gameshow')),
    ('Reality TV',     ('daytime', 'reality', 'dating')),
    ('Anime & Gaming', ('anime', 'gaming', 'video game')),
    ('Creators',       ('creator',)),
    ('Food & Travel',  ('food', 'cook', 'travel')),
    ('Military',       ('military',)),
    ('History',        ('history', 'histoire', 'historia')),
    ('Documentaries',  ('doc', 'dokument', 'biograph', 'educat')),
    ('Science',        ('science', 'astronom')),
    ('Soap Operas',    ('soap',)),
    ('Drama',          ('drama', 'telenovela')),
    ('Nature',         ('nature', 'wildlife', 'animal')),
    ('Faith',          ('faith', 'relig', 'gospel', 'church')),
    ('Westerns',       ('western',)),
    ('Classic TV',     ('classic', 'retro', 'vintage', 'nostalgi')),
    ('Ambiance',       ('ambian', 'relax', 'fireplace')),
    ('Shopping',       ('shopping',)),
    ('Lifestyle',      ('lifestyle', 'fashion', 'home')),
    ('Latino',         ('latino', 'latin', 'espa')),
]

# ---------------------------------------------------------------------------
# 5. NAME KEYWORD RULES -- substring of the channel name (last resort before
# the default). First match wins. A keyword starting with "=" is a whole-word match
# (so "=nfl" skips "Final Conflict", "=mtv" skips KMTV/WMTV local news). Keep
# entries here only for names the pins above do not already catch.
# ---------------------------------------------------------------------------
NAME_KEYWORD_RULES: list[tuple[str, tuple[str, ...]]] = [
    ('Sports',            ('sport', 'deportes', '=nfl')),
    ('Outdoors',          ('=hunting',)),
    ('Music',             ('iheart', '=mtv')),
    ('Ambiance',          ('fireplace',)),
    ('Local News',        ('local news', 'fox local')),
    ('Business',          ('bloomberg', 'finance', 'financial', '=cnbc', 'stock market')),
    ('News',              ('news', 'noticias', 'weather', 'cnn', 'usa today')),
    ('True Crime',        ('investigat', 'detective', 'pluto tv crime')),
    ('Crime',             ('crime', 'murder')),
    ('Food & Travel',     ('cooking', 'kitchen', 'chef')),
    ('Nature',            ('nature',)),
    ('Game Shows',        ('game show',)),
    ('Reality TV',        ('bachelor',)),
    ('Comedy',            ('comedy', 'failarmy')),
    ('Kids',              ('kids', 'family', 'children', 'junior')),
    ('Drama',             ('drama', '=ncis')),
    ('Movies',            ('movie', 'film')),
    ('Food & Travel',     ('travel',)),
    ('History',           ('history',)),
    ('Documentaries',     ('smithsonian',)),
    ('Science',           ('science', '=space')),
]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _kw_hit(kw: str, text: str) -> bool:
    """Substring match; a leading '=' means whole-word match."""
    if kw.startswith('='):
        return re.search(r'(?<![a-z0-9])' + re.escape(kw[1:]) + r'(?![a-z0-9])', text) is not None
    return kw in text


def _first_match(rules: list[tuple[str, tuple[str, ...]]], text: str) -> str | None:
    for label, keywords in rules:
        if any(_kw_hit(kw, text) for kw in keywords):
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


# Source groups deliberately mapped to Entertainment (action/adventure) stay put.
_KEEP_ENTERTAINMENT = re.compile(r'action|adventure')


def category_for_channel(name: str, raw_category: str | None, source_name: str | None = None) -> str | None:
    """Category as it will appear in the output playlist. (source_name is unused; kept for callers.)"""
    name_clean = re.sub(r'(?:\s*\([A-Z]{2}\)|\s*\[DRM\])+\s*$', '', (name or '').strip()).strip()
    name_lower = name_clean.lower()

    # Event feeds look like '[League] Home vs Away | Feed (SRC)': always Sports, whatever the source group.
    if _EVENT_FEED.match(name_clean) and _VS.search(name_lower):
        return 'Sports'

    forced = NAME_OVERRIDES.get(name_lower) or pinned_category(name_clean)
    if forced:                                  # pins beat the raw group and earlier runs
        return forced

    raw_hit = normalize_category(raw_category)
    if raw_hit == DEFAULT_CATEGORY and not _KEEP_ENTERTAINMENT.search(_fold_accents(raw_category or '').lower()):
        # Entertainment is the catch-all (generic source groups, and whatever a previous
        # run wrote back into group-title), so it must not be sticky: let the channel-name
        # keyword rules pull it out before settling for the default.
        return infer_category_from_name(name_lower) or DEFAULT_CATEGORY

    return (
        raw_hit
        or infer_category_from_raw(raw_category)
        or infer_category_from_name(name_lower)
        or DEFAULT_CATEGORY                     # no odd raw group is ever left behind
    )


final_category = category_for_channel

# Square brackets are stripped from every channel's display name / tvg-name.
_BRACKETS = re.compile(r'[\[\]]+')


def _strip_brackets(text: str) -> str:
    return ' '.join(_BRACKETS.sub(' ', text or '').split())


_EVENT_FEED = re.compile(r'\s*\[[^\]]+\]\s*\S')
_VS = re.compile(r'(?<![a-z0-9])vs\.?(?![a-z0-9])')


# ---------------------------------------------------------------------------
# M3U / EPG rewriting
# ---------------------------------------------------------------------------
GROUP_RE = re.compile(r'group-title="([^"]*)"')
TVG_ID_RE = re.compile(r'tvg-id="([^"]*)"')
TVG_NAME_RE = re.compile(r'tvg-name="([^"]*)"')


def _split_extinf(line: str) -> tuple[str, str]:
    """Split at the FIRST comma outside quotes, so names containing commas
    (e.g. "Sister, Sister") stay whole -- same rule as filter_m3u.entry_name."""
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ',' and not in_q:
            return line[:i], line[i + 1:].strip()
    return line, ""


def normalize_m3u(path: Path, dry_run: bool) -> tuple[dict[str, dict[str, str]], Counter]:
    out: list[str] = []
    by_id: dict[str, dict[str, str]] = {}
    changes: Counter = Counter()

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#EXTINF"):
            out.append(line)
            continue

        attrs, name = _split_extinf(line)

        # Category is decided from the ORIGINAL name (event-feed rule needs the brackets).
        m = GROUP_RE.search(attrs)
        old = html.unescape(m.group(1)) if m else None
        new = (category_for_channel(name, old) or old) if m else None

        # Strip brackets from the display name and tvg-name.
        clean = _strip_brackets(name)
        if name and clean and clean != name:
            if line.endswith(name):
                line = line[:-len(name)] + clean
            changes[("(name)", "brackets stripped")] += 1
        tvg = TVG_NAME_RE.search(attrs)
        if tvg:
            tvg_old = html.unescape(tvg.group(1))
            tvg_new = _strip_brackets(tvg_old)
            if tvg_new and tvg_new != tvg_old:
                line = TVG_NAME_RE.sub(
                    lambda _m: f'tvg-name="{tvg_new.replace(chr(34), "&quot;")}"', line, count=1)

        if m and new != old:
            changes[(old, new)] += 1
            line = GROUP_RE.sub(lambda _m: f'group-title="{new.replace(chr(34), "&quot;")}"', line, count=1)

        idm = TVG_ID_RE.search(attrs)
        if idm and m:
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
