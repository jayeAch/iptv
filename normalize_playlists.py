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
#   Reality TV = unscripted reality / competition / dating / game shows
#                (Jersey Shore, Hell's Kitchen, Storage Wars, Deal or No Deal)
#   Romance    = romance movie channels and romance-driven series (Harlequin,
#                Hallmark Movies & More, Romance 365); holiday romance -> Christmas
#   Supernatural Drama = FICTIONAL supernatural series (Charmed, Ghost Whisperer,
#                Teen Wolf, Touched by an Angel); real-event shows stay in Paranormal
# ---------------------------------------------------------------------------
CANONICAL_CATEGORIES: tuple[str, ...] = (
    'Action',
    'Ambiance',
    'Anime & Gaming',
    'Asian',
    'Automotive',
    'Black Entertainment',
    'Broadcast',
    'Christmas',
    'Classic TV',
    'Combat Sports',
    'Comedy',
    'Crime',
    'Documentaries',
    'Drama',
    'Entertainment',
    'Faith',
    'Food & Travel',
    'Game Shows',
    'History',
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
    'Reality TV',
    'Romance',
    'Sci-Fi',
    'Shopping',
    'Soap Operas',
    'Sports',
    'Supernatural Drama',
    'Talk Shows',
    'Toons',
    'True Crime',
    'Westerns',
)

DEFAULT_CATEGORY = 'Entertainment'

# ---------------------------------------------------------------------------
# 1. NAME OVERRIDES -- exact (lowercased) channel name -> category
# ---------------------------------------------------------------------------
NAME_OVERRIDES: dict[str, str] = {
    'comedia': 'Comedy',
    'komedi': 'Comedy',
    'nypd blue': 'Crime',
    'the nanny & mr sheffield - a fine romance': 'Comedy',
    'girlfriends': 'Black Entertainment',
    'lone star': 'Westerns',
    'outflix proud': 'LGB',  # was wrongly caught as a talk show
    'medium': 'Supernatural Drama',  # exact only: 'Long Island Medium' stays Paranormal
    'angel': 'Supernatural Drama',
    'survivor': 'Reality TV',  # exact only: 'Designated Survivor' is a drama
    'clic': 'Latino',          # WHALE, Spanish
    'garfield': 'Latino',      # Roku, Spanish dub
    'danger tv': 'Latino',     # Roku Spanish feed (DangerTV is English)
    'outlaw': 'Westerns',
    'the canyon': 'Westerns',      # Young Riders (Airy)
    'medicine woman': 'Westerns',  # Dr. Quinn
    'sister, sister': 'Black Entertainment',  # Pluto
    'love thy neighbor': 'Black Entertainment',  # Tyler Perry
    'sport': 'Sports',
    'tony robbins network': 'Lifestyle',    # arrive under a "Game Shows" source group
    'growthday network': 'Lifestyle',
    'the judge judy channel': 'Reality TV',
    'hot bench': 'Reality TV',
    'bargain hunt': 'Reality TV',
    'the kids in the hall': 'Comedy',  # sketch show, not a kids channel
    'passions': 'Soap Operas',        # exact only: too generic for a whole-word pin
    'loving': 'Soap Operas',
    'santa barbara': 'Soap Operas',   # exact only: local-news channels carry the city name
    'another world': 'Soap Operas',
    'sunset beach': 'Soap Operas',
    'port charles': 'Soap Operas',
    'pluto tv retro action movies': 'Movies',  # Classic TV pin ('pluto tv retro') would take it
    'at home with family handyman': 'Lifestyle',  # 'family' name keyword would make it Kids
    'the fbi': 'Classic TV',          # 1965 series; 'The FBI Files' stays True Crime
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


NAME_PINS: dict[str, tuple[str, ...]] = {
    # Alphabetical by category so blocks are easy to find. Matching PRECEDENCE is NOT
    # the order here: it is PIN_ORDER below. When you add a block, add its key to
    # PIN_ORDER too (an assert checks both lists agree).
    # Classic action-adventure TV series. Sits above Crime in PIN_ORDER so Walker Texas
    # Ranger (formerly a Crime pin) and similar shows land here. Action movie channels
    # ('Pluto TV Action Movies') are not listed: they stay in Movies.
    'Action': _words("""
        walker texas ranger
        walker, texas ranger
        universal action
        all action
        action plus
        pluto tv 80's action
        xumo free action
        macgyver
        renegade
        knight rider
        the a-team
        airwolf
        street hawk
        the fall guy
    """),
    # Stingray's ambient/wellness video channels (its music channels stay in Music).
    'Ambiance': _words("""
        stingray naturescape
        pluto tv peis
        pluto tv pejs
        pluto tv eldstad
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
    """),
    # Anime and video games. Above Toons and Sci-Fi (Pluto TV Anime, Sonic, Super Mario
    # would otherwise land there). Estrella Games is Latino (pinned first).
    'Anime & Gaming': _words("""
        anime
        animeverse
        caylus
        avatar
        anime all day
        crunchyroll
        hidive
        retrocrush
        funimation
        animax
        tokyopop
        yu-gi-oh
        naruto
        dragon ball
        sailor moon
        gundam
        mecha
        gaming
        gamer
        gamers
        sensical gaming
        pocket.watch game-on
        game-on
        twitch
        ign
        gamespot
        game tv
        retro gaming
        arcade
        speedrun
        minecraft
        fortnite
        roblox
        nintendo
        playstation
        xbox
        atari
        pac-man
        sonic the hedgehog
        super mario brothers
    """),
    # Asian content (K-drama, Bollywood, TVB, Asian cinema). Anime stays in Anime & Gaming.
    'Asian': _words("""
        amasian tv
        aliwko
        goldentv
        asiancrush
        asian drama
        bollywood
        bollywood 4u
        bollywood tv
        shemaroo bollywood
        cj enm k-stories
        genie k-drama
        genie k-movie
        jubao
        k-drama
        k.movies
        kmovies
        kfood
        korean
        kocowa
        rakuten viki
        series k edge
        series k heart
        series k legacy
        thailand tv
        joysauce network
        tvb pearl drama
    """),
    # Car culture: build/restoration shows, auctions, car-review shows. Live racing
    # (F1, NASCAR, MotoGP, Racer, etc.) stays in Sports above. Sits before Outdoors.
    'Automotive': _words("""
        top gear
        petrolheads
        pluto tv petrolheads
        mecum
        mecum tv
        classic car auctions
        classic car
        car auction
        car auctions
        barrett-jackson
        motortrend
        motor trend
        torque
        pimp my ride
        powernation
        wheeler dealers
        overhaulin
        fast n loud
        counting cars
        hagerty
        car and driver
        roadkill
        junkyard digs
        graveyard carz
    """),
    # Channels marketed to Black audiences. Sits after Faith (so BET Gospel stays
    # Faith) and above Paranormal/True Crime/Crime/Comedy/Drama so TV One Crime &
    # Justice, BET Throwbacks etc. land here. Whole-word match: 'bet' catches every
    # "BET ..." channel; 'tv one' catches the TV One channels.
    'Black Entertainment': _words("""
        bet
        tv one
        allblk
        beyond the gates
        allblk gems
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
        nolly africa hd
        tidpix
        tidpix-authentically african
        demand africa
    """),
    # Christmas / holiday channels. Right after Latino so they beat Movies, Drama,
    # Romance and Faith ("Hallmark Movies & More Holiday Favorites" -> Christmas).
    'Christmas': _words("""
        christmas
        xmas
        pluto tv jul
        pluto tv de bedste julespecials
        pluto tv de beste julespesialene
        pluto tv julehygge
        pluto tv julekos
        pluto tv julmys
        pluto tv mistletoe
        x-mas
        andersens julehemmelighed
        holiday
        holidays
        merry christmas
        hearties christmas
        always christmas
        christmas 365
        christmas plus
        thats christmas
        yule
        yuletide
        santa
        santa claus
    """),
    # Classic TV: vintage live-action network shows and the Classic/Retro channels.
    # Above Comedy/Crime/Drama/Toons so e.g. I Love Lucy is not filed as Comedy.
    # Westerns, Black Entertainment, Sports etc. sit higher and still win. Add a show
    # here to force it into Classic TV (apostrophes are stripped: hogans heroes).
    'Classic TV': _words("""
        classic tv
        three's company
        classic television
        vintage tv
        retro tv
        pluto tv retro
        nostalgia tv
        tv land
        i love lucy
        the lucy show
        dick van dyke
        mary tyler moore
        the andy griffith show
        andy griffith
        the beverly hillbillies
        beverly hillbillies
        green acres
        petticoat junction
        gilligans island
        the munsters
        the addams family
        bewitched
        i dream of jeannie
        my three sons
        the brady bunch
        happy days
        laverne & shirley
        mork & mindy
        leave it to beaver
        father knows best
        the honeymooners
        hogans heroes
        f troop
        get smart
        the monkees
        gomer pyle
        lassie
        family affair
        that girl
        the love boat
        fantasy island
        charlies angels
        the carol burnett show
        the ed sullivan show
        johnny carson
        the three stooges
        three stooges
        abbott and costello
        mission impossible
        all in the family
    """),
    # Combat sports first so these beat the Kung Fu Flicks and Sports pins below.
    'Combat Sports': _words("""
        mma
        ufc
        swerve combat
        fight network
        one championship
        one championship tv
        glory kickboxing
    """),
    # Must sit ABOVE Crime: the Crime list has the whole word 'mystery', which
    # would otherwise capture "Mystery Science Theater 3000".
    'Comedy': _words("""
        mystery science theater 3000
        mystery science theater
        mst3k
    """),
    'Comedy (after Classic TV)': _words("""
        comedy central
        according to jim
        pluto tv comedy
        pluto tv komedi
        pluto tv komedie
        komedi
        the daily show
        komedie
        the nanny
        rifftrax
        just for laughs comedy
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
        nash bridges
        the practice
        columbo
        miami vice
        prime suspect
        crime thrillher
        pluto tv crime thrillers
        pluto tv crime drama
        pluto tv crime movies
        criminal minds
        csi
        jag
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
        perry mason
        poirot
        rockford files
        sherlock
        whodunit
    """),
    # Documentary channels, incl. Scandinavian spellings (accents are folded, so
    # dokumentär == dokumentar). History/Nature/True Crime sit in their own lists.
    'Documentaries': _words("""
        documentary
        documentaries
        documentary channel
        documentaries channel
        docurama
        docurama tv
        curiosity stream
        curiositystream
        real stories
        pluto tv docs
        pluto tv documentaries
        pluto tv dokumentar
        pluto tv dokumentarer
        dokumentar
        dokumentarer
        dokumentarfilm
        dokumentarfilmer
        dokumentarkanal
        dokumentarkanalen
        dokumentarserier
        dokumentti
        dokumentit
        dokumenttielokuvat
    """),
    # Non-crime dramas (crime dramas stay in Crime above).
    'Drama': _words("""
        more tv drama
        love boat
        nashville
        filmrise drama
        felicity
        nip/tuck
        pluto tv drama
        bbc drama
        vikings
        7th heaven
        beverly hills 90210
        dawsons creek
        everwood
        gilmore girls
        heartland
        highway to heaven
        little house on the prairie
        one tree hill
        st. elsewhere
        the waltons
    """),
    # Religious / devotional channels. Whole-word matching, so keep entries
    # specific (a bare 'faith' or 'jesus' would hit Judge Faith / Black Jesus).
    'Faith': _words("""
        bet gospel
        jewish life tv
        trinity broadcast network
        cbn
        catholic tv
        christian youth
        cowboy christian network
        daystar
        daystar tv
        daystartv
        elevation church
        ewtn
        faithnfreedomtv
        faithope
        god tv
        gospel truth
        hope channel
        impact gospel
        islam channel
        joel osteen network
        juventud renovada en el espiritu santo
        living faith tv
        sikh ratnavali
        tbn
        the 700 club
        trinity broadcasting network
    """),
    # Food and travel channels. Below Reality TV (Hell's Kitchen, Kitchen Nightmares stay
    # there) and above Paranormal/Documentaries/Comedy/Drama. Kept specific: no bare
    # 'travel' or 'kitchen' (Time Travel, Kitchen Cabinet). Pluto DK cooking/dinner shows
    # (Koch'en på toppen, Til Middag Hos, Med kniven for struben) are food too.
    'Food & Travel': _words("""
        pluto tv food
        food network
        tastemade
        emeril lagasse
        tareq taylors nordic cookery
        nordic cookery
        sweet escapes
        kochen pa toppen
        kocken pa toppen
        til middag hos
        med kniven for struben
        cooking channel
        travel channel
        travelxp
        travel + leisure
        travel and leisure
        journy
        rvtv
        30a tv
        drone tv
    """),
    # Game shows. Sits above Reality TV so these no longer land there. After Sports/
    # Outdoors so 'American Ninja Warrior' stays Sports.
    'Game Shows': _words("""
        classic game shows
        pluto tv game shows
        gsn
        buzzr
        deal or no deal
        deal or no deal usa
        family feud
        wheel of fortune
        jeopardy
        the price is right
        press your luck
        match game
        lets make a deal
        pyramid
        hollywood squares
        card sharks
        supermarket sweep
        love connection
        newlywed game
        the newlywed game
        dating game
        the dating game
        tic tac dough
        whats my line
        to tell the truth
        ive got a secret
        beat the clock
        jokers wild
        hollywood game night
        celebrity name game
        the celebrity name game channel
        name that tune
        weakest link
        the weakest link
        who wants to be a millionaire
        millionaire
        minute to win it
        gong show
        the gong show
        cash cab
        ben steins money
        catchphrase
        trivia
        bingo
    """),
    # History. After True Crime and Paranormal (Ancient Aliens, Curse
    # of Oak Island stay Paranormal) and above Crime/Drama ("History's Mysteries" -> History).
    # 'Xtreme Outdoor by History' stays Outdoors (pinned earlier).
    'History': _words("""
        history
        historic
        historical
        history channel
        history hit
        history hit tv
        history unsealed
        history and warfare
        autentic history
        warfare
        civil war
        american heroes channel
        medieval
        timeline
    """),
    'Horror': _words("""
        dread tv
        frightflix
        horror
        itn's creatureverse
        pluto tv terror
        scare o ween
        scares
        scream factory
        screambox
        shudder
        watch it scream
    """),
    # Children's / preschool channels. LAST so Toons (Cartoon Network, Boomerang, Toon
    # Goggles) and Comedy win first. Keep 'family' out: Family Feud, Addams Family etc.
    # 'The Kids in the Hall' is a NAME_OVERRIDE (Comedy).
    'Kids': _words("""
        kids
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
        baby shark tv
        babyfirst
        babyfirst tv
        baby tv
        babytv
        henry danger
        cocomelon
        little baby bum
        moonbug
        moonbug kids
        ryan and friends
        ryans world
        ninja kidz tv
        nick jr
        nickelodeon
        disney junior
        pbs kids
        sesame street
        paw patrol
        peppa pig
        bluey
        blippi
        pocoyo
        caillou
        thomas and friends
        thomas & friends
        barney
        teletubbies
        wiggles
        the wiggles
        dora the explorer
        curious george
        strawberry shortcake
        care bears
        my little pony
        power rangers
        cartoonito
        kartoon channel
        kidoodle
        kidoodle.tv
        kidsflix
        super simple
        super simple songs
        mother goose club
        genius brands
        kabillion
        universal kids
        sprout
        noggin
        chuggington
        veggietales
        veggie tales
        fraggle rock
        mister rogers
        mr rogers
        rugrats
        hey arnold
        toddler
        toddlers
        preschool
    """),
    # Martial arts next so MMA/UFC/kickboxing beat the generic Sports pins below.
    'Kung Fu Flicks': _words("""
        kung fu
        flicks of fury
        kungfu
        martial art
        hi-yah!
        martial arts
        wu tang collection
        wu-tang collection
        shaw brothers
        bruce lee
        jackie chan
        hong kong fight club
    """),
    # Spanish-language channels, any genre. FIRST so "CSI en Espanol", "ViX Deportes",
    # "Telemundo Noticias" etc. beat the English-name pins below (csi, cops, sports...).
    'Latino': _words("""
        spanish
        en espanol
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
        mi raza canal plus
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
        lgb
        lgbt
        lgbtq
        lgbtq+
        the l word
        logo pluto tv
        lgbtqia
        outflix
        outflix movies
        outflix proud
        pluto tv pride
        pride tv
        pride channel
        pride movies
        here tv
        here tv lgbtq
        dekkoo
        revry
        revry tv
        gaydio
        gay tv
        gay movies
        queer
    """),
    # Military: above History so military-history channels land here. Whole-word match;
    # keep entries specific ('heroes' alone would catch Hogan's Heroes).
    'Military': _words("""
        military
        military history
        military heroes
        combat war channel
        war channel
        war movies
        war films
        world war ii
        world war 2
        wwii
        ww2
        navy seals
        band of brothers
    """),
    # Movie channels. LAST pin: Comedy/Crime/Horror/Sci-Fi/Kids/Christmas/Romance/Westerns
    # etc. win first, so 'Pluto TV Comedy Movies' stays Comedy. Catches movie channels that
    # arrive under a generic group (Entertainment, Sci-Fi) like 'FilmRise Free Movies'.
    'Movies': _words("""
        movie
        movies
        film
        films
        cinema
        filmrise action
        action hollywood movies
    """),
    # Nature / wildlife / pets. ABOVE History: Samsung files these under its "History"
    # group. (Nature is in category_exclude_list.py, so these are dropped from output.)
    'Nature': _words("""
        pbs nature
        terra mater wild
        adventure earth
        nat geo
        go wild
        brave wilderness
        dog whisperer
        dinos 24/7
        earthday 365
        bbc earth
        wildearth
        the yorkshire vet
        weatherspy
        the pet collective
        samsung wild life
        lucky dog
        love nature
        love nature 4k
        rovr pets
        the jack hanna channel
    """),
    'Outdoors': _words("""
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
        survivorman
        life below zero
        inwild
        inwonder
    """),
    'Paranormal': _words("""
        pluto tv overnaturlig
        a haunting
        alien files
        unxplained zone
        pluto tv alien invasion
        alien nation
        ancient aliens
        bigfoot
        mysteries xplored
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
        ghosts are real
        mysterious worlds
        psychic kids
        the dead files
        the unexplained
        the curse of oak island
        the unxplained with william shatner
        ufo files
        ufo hunters
        unexplained files
        unexplained mysteries
    """),
    # Unscripted reality / competition / dating. (Game shows have their own category.) After Black Entertainment
    # (Growing Up Hip Hop etc. stay there) and Automotive/Outdoors/Sports (Fast N' Loud,
    # Life Below Zero, American Ninja Warrior). Bare 'survivor' is a NAME_OVERRIDE.
    'Reality TV': _words("""
        reality
        emergency rescue
        paradise hotel
        the osbournes
        ice road truckers
        untold stories of the
        chaos on cam
        earthxtra
        supernanny
        bad girls club
        grind hard plumbing co
        extreme living
        pluto tv urbex
        car chase
        extreme jobs
        tiny house nation
        the therapy couch
        reality tv
        wicked tuna
        deal masters
        dog the bounty hunter
        deal zone
        antiques road trip
        antiques roadshow
        judge judy
        hot bench
        all weddings we tv
        amc reality
        real housewives
        real housewives vault
        bravo vault
        e! keeping up
        keeping up with the kardashians
        kardashians
        the hills
        jersey shore
        geordie shore
        ex on the beach
        made in chelsea
        towie
        the only way is essex
        love island
        big brother
        the bachelor
        the bachelorette
        bachelor in paradise
        too hot to handle
        temptation island
        married at first sight
        matched married meet
        90 day fiance
        first dates
        dating naked
        catfish
        the challenge
        the real world
        road rules
        teen mom
        dance moms
        wife swap
        undercover boss
        cheaters
        intervention
        hoarders
        botched
        below deck
        vanderpump rules
        gordon ramsay
        hells kitchen
        kitchen nightmares
        bar rescue
        masterchef
        top chef
        chopped
        pluto tv tough jobs
        cake boss
        come dine with me
        project runway
        americas got talent
        got talent
        american idol
        the voice
        shark tank
        dragons den
        the amazing race
        fear factor
        wipeout
        bondi rescue
        wipeout xtra
        extreme makeover
        extreme makeover home edition
        storage wars
        auction hunters
        pawn stars
        american pickers
        gold rush
        deadliest catch
        swamp people
        ax men
        ice pilots
        duck dynasty
        naked and afraid
    """),
    # Romance movie channels and romance-driven series. After Christmas (holiday romance
    # -> Christmas) and Latino (Cine Romantico), above Drama/Movies.
    'Romance': _words("""
        romance
        pluto tv romantik
        romantic
        romance 365
        great american romcoms
        free romance network
        movie hub romance
        pluto tv romance
        pluto tv harlequin
        harlequin
        light & love
        spark tv light & love
        love & drama
        lifetime love & drama
        lifetime movies love & drama
        hallmark movies & more
        hallmark movies and more
        romcom
        rom-com
        rom com
        romantic comedy
        romantic comedies
        love stories
        bridal
        a fine romance
    """),
    # Sci-Fi above Horror: anything both is Sci-Fi. These sit above the raw
    # group lookup so channels sourced under a generic "Movies" group still
    # land in their genre.
    'Sci-Fi': _words("""
        the asylum
        the twilight zone
        mutant x
        unidentified
        beyond belief
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
    """),
    # Home-shopping channels. Whole-word match, so keep entries specific.
    'Shopping': _words("""
        shopping
        home shopping
        tv shopping
        teleshopping
        shopping network
        the shopping channel
        qvc
        qvc2
        qvc plus
        hsn
        hsn2
        shophq
        shop hq
        shoplc
        shop lc
        shop tv
        tv shop
        shop at home
        jewelry television
        gem shopping network
        evine
        ideal world
        shop the world
    """),
    # Soap operas. Above Classic TV and Drama (both used to hold them). Whole-word
    # match; ambiguous one-word titles (Passions, Loving, Santa Barbara) are in
    # NAME_OVERRIDES. 'dynasty' is safe: Duck Dynasty is pinned to Reality TV above.
    'Soap Operas': _words("""
        soaps
        soap opera
        eastenders
        soap operas
        daytime soaps
        primetime soaps
        all my children
        one life to live
        general hospital
        days of our lives
        the young and the restless
        the bold and the beautiful
        guiding light
        as the world turns
        search for tomorrow
        the edge of night
        dynasty
        falcon crest
        knots landing
        melrose place
        peyton place
    """),
    'Sports': _words("""
        sport
        sports
        sports channel
        great by choice
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
    """),
    # Fictional supernatural series. After Paranormal ('Long Island Medium', 'Ghost Hunters'
    # stay there) and above Sci-Fi/Drama/Comedy. Bare 'medium'/'angel' are NAME_OVERRIDES.
    'Supernatural Drama': _words("""
        supernatural
        highway to heaven
        supernatural drama
        from dusk till dawn: the series
        charmed
        ghost whisperer
        touched by an angel
        teen wolf
        sabrina the teenage witch
        chilling adventures of sabrina
        van helsing
        buffy
        buffy the vampire slayer
        angel the series
        vampire diaries
        the vampire diaries
        the originals
        legacies
        true blood
        sleepy hollow
        grimm
        teen wolf
        roswell
        roswell new mexico
        forever knight
        moonlight
        dark shadows
        beauty and the beast
        the secret circle
        witches of east end
        lost girl
        being human
        the 4400
        eastwick
        the lost room
        medium
        wolf lake
        point pleasant
        reaper
        dead like me
        pushing daisies
        wonderfalls
    """),
    # Tabloid / daytime talk. Judge shows and late night are not included.
    'Talk Shows': _words("""
        jerry springer
        drew barrymore show
        springer
        maury
        steve wilkos
        geraldo
        geraldo rivera
        jenny jones
        ricki lake
        montel
        montel williams
        sally jessy raphael
        jeremy kyle
        povich
        nosey
        dr. phil
    """),
    # Toons above Comedy/Kids: animated shows (incl. adult animation) stay here.
    'Toons': _words("""
        aqua teen hunger force
        pluto tv animation
        archer
        animation+
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
    """),
    'True Crime': _words("""
        60 days in jail
        20/20
        crime 360
        law&crime
        buzzfeed unsolved
        real crime
        jail
        60 days in by a&e
        witness to justice
        medical examiner
        live channel
        filmrise mysteries
        i survived
        mysteria
        a&e alaska state troopers
        dark side of
        trublu
        trublu crime channel
        american justice
        body cam
        cold case files
        cold justice
        cops
        48 hours
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
    """),
    # Westerns. Sits above Drama (Dr. Quinn) and Classic TV. Whole-word match:
    # 'western' catches Western TV, Western Bound, FilmRise Western, a-z Western
    # Grit, etc. Generic names (Lone Star, Outlaw, The Canyon) are in NAME_OVERRIDES.
    'Westerns': _words("""
        western
        grit xtra
        circle
        circle country
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
        wrangled by insp
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
    'Automotive',
    'Outdoors',
    'Ambiance',
    'Faith',
    'Black Entertainment',
    'Westerns',
    'Shopping',
    'Game Shows',
    'Anime & Gaming',
    'Reality TV',
    'Food & Travel',
    'Romance',
    'Paranormal',
    'Supernatural Drama',
    'True Crime',
    'Nature',
    'Military',
    'History',
    'Documentaries',
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
        'ambiance', 'mood + ambiance',
    ),
    'Anime & Gaming': (
        'anime', 'anime & animation', 'anime & gaming', 'animation & anime',
        'gaming', 'gaming & anime', 'games', 'video games',
    ),
    'Asian': (
        'asian', 'asian drama', 'bollywood', 'k-drama', 'k-movie', 'korean',
    ),
    'Automotive': (
        'auto & motor',
    ),
    'Christmas': (
        'christmas', 'christmas movies', 'holiday', 'holidays', 'holiday movies',
        'xmas',
    ),
    'Classic TV': (
        'classic', 'classic tv', 'classic tv & movies', 'classics', 'nostalgia',
        'retro', 'vintage tv', 'westerns + classics',
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
    'Documentaries': (
        'biographies', 'biography', 'docs', 'documentaires', 'documentales',
        'documentari', 'documentaries', 'documentary', 'documentary & biography',
        'dokumentar', 'dokumentarer', 'dokumentarfilm', 'dokumentarfilmer',
        'dokumentär', 'dokumentärer', 'dokumentärfilm', 'dokumentärfilmer',
        'dokumentti', 'dokumentit', 'dokumenttielokuvat',
        'dokus & wissen', 'education', 'educational', 'factual',
        'history + docs', 'infotainment', 'science', 'special interest',
        'true stories',
    ),
    'Drama': (
        'drama series', 'dramas', 'serie',
        'serier', 'series', 'tv dramas',
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
    'Game Shows': (
        'game show', 'game shows', 'gameshows',
    ),
    'History': (
        'history', 'histoire', 'historia', 'storia', 'geschichte',
    ),
    'Horror': (
        'horror', 'scary',
    ),
    'Kids': (
        'bambini', 'children', 'childrens', 'jeunesse', 'kids', 'kids & family',
        'kids + family', 'kids and family', 'niños',
    ),
    'Kung Fu Flicks': (
        'kung fu', 'martial arts',
    ),
    'Latino': (
        'en español', 'latino',
    ),
    'LGB': (
        'lgb', 'lgbt', 'lgbtq', 'lgbtq+', 'lgbtqia', 'lgbtq+ & pride', 'pride',
    ),
    'Lifestyle': (
        'culture + lifestyle', 'home', 'home & diy', 'home & food', 'lifestyle',
    ),
    'Local News': (
        'local channels', 'local news', 'regional news',
    ),
    'Military': (
        'military', 'military & war', 'war', 'war movies',
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
        'daytime tv & games', 'dating', 'reality', 'reality shows',
        'reality tv',
    ),
    'Romance': (
        'romance', 'romance movies', 'romantic', 'romantic comedy',
    ),
    'Sci-Fi': (
        'adrenaline & sci-fi', 'fantasy', 'horror & sci-fi', 'sci fi', 'scifi',
        'sci-fi', 'sci-fi & fantasy', 'sci-fi & horror', 'sci-fi & supernatural',
        'sci-fi + fantasy', 'sci-fi and horror', 'science fiction',
    ),
    'Soap Operas': (
        'daytime soaps', 'primetime soaps', 'soap opera', 'soap operas', 'soaps',
    ),
    'Supernatural Drama': (
        'supernatural', 'supernatural drama',
    ),
    'Toons': (
        'animated', 'animation', 'cartoon', 'cartoons', 'toons',
    ),
    'Shopping': (
        'home shopping', 'shop', 'shopping', 'teleshopping', 'tv shopping',
    ),
    'Sports': (
        'calcio', 'deporte', 'motor sports', 'sport', 'sports', 'sports & outdoors',
        'sports on now',
    ),
    'Westerns': (
        'western', 'westerns',
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
    ('News',           ('news', 'business', 'finance', 'weather')),
    ('Movies',         ('movie', 'film', 'cinema')),
    ('Toons',          ('cartoon', 'toons', 'animation')),
    ('Kids',           ('kid', 'family')),
    ('Sports',         ('sport', 'soccer', 'football', 'basketball')),
    ('Music',          ('music', 'radio')),
    ('Game Shows',     ('game show', 'gameshow')),
    ('Reality TV',     ('daytime', 'reality', 'dating')),
    ('Anime & Gaming', ('anime', 'gaming', 'video game')),
    ('Food & Travel',  ('food', 'cook', 'travel')),
    ('Military',       ('military',)),
    ('History',        ('history', 'histoire', 'historia')),
    ('Documentaries',  ('doc', 'dokument', 'science', 'biograph', 'educat')),
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
# the default). First match wins. Trailing spaces are intentional.
#   - Local News precedes News.
#   - True Crime precedes Crime (so "law & crime" is True Crime).
#   - Sci-Fi precedes Horror (so anything both is Sci-Fi).
#   - Christmas/LGB sit near the top so they beat Movies/Kids/Drama; Romance and
#     Supernatural Drama precede Drama/Movies.
# ---------------------------------------------------------------------------
NAME_KEYWORD_RULES: list[tuple[str, tuple[str, ...]]] = [
    ('Latino',         ('en espanol', 'en español', 'spanish', 'telemundo', 'novelas')),
    ('Christmas',      ('christmas', 'xmas', 'holiday')),
    ('LGB',            ('lgbt', 'outflix', 'pluto tv pride')),
    ('Asian',          ('k-drama', 'bollywood', 'korean', 'asiancrush')),
    ('Talk Shows',     ('jerry springer', 'maury', 'dr. phil', 'nosey')),
    ('Kung Fu Flicks', ('kung fu', 'martial art', 'kickboxing')),
    ('Sports',         ('sport', 'deportes', 'nfl', 'nba ', 'nhl', 'mlb', 'nascar')),
    ('Automotive',     ('top gear', 'motortrend', 'motor trend', 'petrolhead', 'car auction', 'classic car')),
    ('Outdoors',       ('outdoor', 'fishing', 'hunting')),
    ('Music',          ('iheart', 'vevo', 'stingray', 'mtv ', 'concerts')),
    ('Ambiance',       ('ambient', 'fireplace', 'yule log', 'relaxation', 'lofi', 'slow tv')),
    ('Local News',     ('local news', 'fox local')),
    ('News',           ('news', 'noticias', 'weather', 'cnn', 'usa today', 'bloomberg')),
    ('True Crime',     ('true crime', 'court tv', 'cops', 'law & crime', 'forensic', 'dateline',
                        'first 48', 'cold case', 'investigat', 'detective', 'pluto tv crime')),
    ('Paranormal',     ('paranormal', 'ghost hunt', 'haunted', 'bigfoot', 'ufo ', 'ancient aliens',
                        'unexplained')),
    ('Supernatural Drama', ('supernatural', 'charmed', 'ghost whisperer')),
    ('Crime',          ('crime', 'mystery', 'murder', 'whodunit')),
    ('Sci-Fi',         ('sci-fi', 'star trek', 'stargate', 'science fiction')),
    ('Horror',         ('horror', 'scary', 'screambox', 'haunt')),
    ('Anime & Gaming', ('anime', 'crunchyroll', 'retrocrush', 'hidive', 'yu-gi-oh', 'gaming')),
    ('Food & Travel',  ('food network', 'tastemade', 'cooking', 'kitchen', 'chef')),
    ('Nature',         ('nature', 'wildlife', 'wildearth', 'love nature', 'national geographic')),
    ('Lifestyle',      ('this old house', 'home & diy', 'gardening', 'home improvement')),
    ('Game Shows',     ('game show', 'gameshow', 'family feud', 'jeopardy', 'wheel of fortune')),
    ('Reality TV',     ('reality', 'real housewives', 'bachelor', 'love island', 'big brother')),
    ('Romance',        ('romance', 'romantic', 'harlequin', 'hallmark movies')),
    ('Comedy',         ('comedy', 'laugh', 'sitcom', 'snl', 'mst3k', 'failarmy')),
    ('Toons',          ('cartoon', 'toonami', 'toon goggles', 'looney tunes')),
    ('Kids',           ('kids', 'family', 'children', 'junior')),
    ('Soap Operas',    ('soap opera',)),
    ('Drama',          ('drama', 'hallmark', 'law & order', 'csi', 'ncis')),
    # Before Movies so 'dokumentarfilm' is not caught by 'film'.
    ('Documentaries',  ('documentar', 'dokumentar', 'dokumentär', 'dokumentti')),
    ('Movies',         ('movies', 'movie', 'cinema', 'film', 'paramount movie')),
    ('Westerns',       ('western', 'gunsmoke', 'wild west', 'bonanza')),
    ('Food & Travel',  ('travel', 'journy', 'travelxp')),
    ('Military',       ('military', 'war channel')),
    ('History',        ('history', 'historic', 'warfare')),
    ('Documentaries',  ('smithsonian', 'science', 'mythbusters', 'nasa+', 'space')),
    ('Shopping',       ('shopping', 'qvc', 'hsn', 'shop lc', 'shophq', 'shoplc', 'jewelry television')),
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


# Source groups deliberately mapped to Entertainment (action/adventure) stay put.
_KEEP_ENTERTAINMENT = re.compile(r'action|adventure')


def category_for_channel(name: str, raw_category: str | None, source_name: str | None = None) -> str | None:
    """Category as it will appear in the output playlist. (source_name is unused; kept for callers.)"""
    name_clean = re.sub(r'(?:\s*\([A-Z]{2}\)|\s*\[DRM\])+\s*$', '', (name or '').strip()).strip()
    name_lower = name_clean.lower()

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


# ---------------------------------------------------------------------------
# M3U / EPG rewriting
# ---------------------------------------------------------------------------
GROUP_RE = re.compile(r'group-title="([^"]*)"')
TVG_ID_RE = re.compile(r'tvg-id="([^"]*)"')


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
