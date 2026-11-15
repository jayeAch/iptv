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
#   Latino     = Spanish-language channels (any genre: novelas, news, movies, ViX, Telemundo)
#   Asian      = K-drama, Bollywood, C-drama/TVB, Asian cinema and Asian-focused channels
#                (anime stays in Anime & Gaming, martial arts in Kung Fu Flicks)
#   Talk Shows = tabloid/daytime talk (Springer, Maury, Wilkos, Geraldo, Nosey, Dr. Phil)
#   Automotive = car culture: build/restoration shows, auctions, car-review shows
#                (Top Gear, Mecum, PetrolHeads); live racing stays in Sports
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
#   History    = history/military-history channels and shows (History Hit, History
#                Unsealed, Military Heroes); pinned above Crime/Drama so they win
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
    'Documentary',
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
    'Movies',
    'Music',
    'Nature',
    'News',
    'Outdoors',
    'Paranormal',
    'Reality TV',
    'Romance',
    'Sci-Fi',
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
    'the kids in the hall': 'Comedy',  # sketch show, not a kids channel
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
    # Spanish-language channels, any genre. FIRST so "CSI en Espanol", "ViX Deportes",
    # "Telemundo Noticias" etc. beat the English-name pins below (csi, cops, sports...).
    ('Latino', _words("""
        spanish
        en espanol
        telemundo
        vix
        canela.tv
        novelas
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
    """)),
    # Christmas / holiday channels. Right after Latino so they beat Movies, Drama,
    # Romance and Faith ("Hallmark Movies & More Holiday Favorites" -> Christmas).
    ('Christmas', _words("""
        christmas
        xmas
        x-mas
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
    """)),
    # LGBTQ+ channels. Keep 'pride' specific: a bare 'pride' would hit Pride & Prejudice.
    ('LGB', _words("""
        lgb
        lgbt
        lgbtq
        lgbtq+
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
    """)),
    # Asian content (K-drama, Bollywood, TVB, Asian cinema). Anime stays in Anime & Gaming.
    ('Asian', _words("""
        amasian tv
        aliwko
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
    """)),
    # Tabloid / daytime talk. Judge shows and late night are not included.
    ('Talk Shows', _words("""
        jerry springer
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
    """)),
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
    # Car culture: build/restoration shows, auctions, car-review shows. Live racing
    # (F1, NASCAR, MotoGP, Racer, etc.) stays in Sports above. Sits before Outdoors.
    ('Automotive', _words("""
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
    # Religious / devotional channels. Whole-word matching, so keep entries
    # specific (a bare 'faith' or 'jesus' would hit Judge Faith / Black Jesus).
    ('Faith', _words("""
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
    """)),
    # Channels marketed to Black audiences. Sits after Faith (so BET Gospel stays
    # Faith) and above Paranormal/True Crime/Crime/Comedy/Drama so TV One Crime &
    # Justice, BET Throwbacks etc. land here. Whole-word match: 'bet' catches every
    # "BET ..." channel; 'tv one' catches the TV One channels.
    ('Black Entertainment', _words("""
        bet
        tv one
        allblk
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
    """)),
    # Westerns. Sits above Drama (Dr. Quinn) and Classic TV. Whole-word match:
    # 'western' catches Western TV, Western Bound, FilmRise Western, a-z Western
    # Grit, etc. Generic names (Lone Star, Outlaw, The Canyon) are in NAME_OVERRIDES.
    ('Westerns', _words("""
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
    """)),
    # Game shows. Sits above Reality TV so these no longer land there. After Sports/
    # Outdoors so 'American Ninja Warrior' stays Sports.
    ('Game Shows', _words("""
        game show
        game shows
        gameshow
        gameshows
        gameshow tv
        game show network
        game show central
        game show marathon
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
    """)),
    # Anime and video games. Above Toons and Sci-Fi (Pluto TV Anime, Sonic, Super Mario
    # would otherwise land there). Estrella Games is Latino (pinned first).
    ('Anime & Gaming', _words("""
        anime
        animeverse
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
    """)),
    # Unscripted reality / competition / dating. (Game shows have their own category.) After Black Entertainment
    # (Growing Up Hip Hop etc. stay there) and Automotive/Outdoors/Sports (Fast N' Loud,
    # Life Below Zero, American Ninja Warrior). Bare 'survivor' is a NAME_OVERRIDE.
    ('Reality TV', _words("""
        reality
        reality tv
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
    """)),
    # Romance movie channels and romance-driven series. After Christmas (holiday romance
    # -> Christmas) and Latino (Cine Romantico), above Drama/Movies.
    ('Romance', _words("""
        romance
        romantic
        romance 365
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
    """)),
    ('Paranormal', _words("""
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
    """)),
    # Fictional supernatural series. After Paranormal ('Long Island Medium', 'Ghost Hunters'
    # stay there) and above Sci-Fi/Drama/Comedy. Bare 'medium'/'angel' are NAME_OVERRIDES.
    ('Supernatural Drama', _words("""
        supernatural
        supernatural drama
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
    """)),
    # History / military history. After True Crime and Paranormal (Ancient Aliens, Curse
    # of Oak Island stay Paranormal) and above Crime/Drama ("History's Mysteries" -> History).
    # 'Xtreme Outdoor by History' stays Outdoors (pinned earlier).
    ('History', _words("""
        history
        historic
        historical
        history channel
        history hit
        history hit tv
        history unsealed
        history and warfare
        autentic history
        military history
        military heroes
        warfare
        world war ii
        world war 2
        wwii
        ww2
        civil war
        american heroes channel
        medieval
        timeline
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
        filmrise drama
        nip/tuck
        pluto tv drama
        vikings
        7th heaven
        beverly hills 90210
        dawsons creek
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
        itn's creatureverse
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
        pluto tv comedy
        pluto tv komedi
        pluto tv komedie
        komedi
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
    """)),
    # Children's / preschool channels. LAST so Toons (Cartoon Network, Boomerang, Toon
    # Goggles) and Comedy win first. Keep 'family' out: Family Feud, Addams Family etc.
    # 'The Kids in the Hall' is a NAME_OVERRIDE (Comedy).
    ('Kids', _words("""
        kids
        kidz
        totally turtles
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
    'Documentary': (
        'biographies', 'biography', 'docs', 'documentaires', 'documentales',
        'documentari', 'documentaries', 'documentary & biography',
        'dokus & wissen', 'education', 'educational', 'factual',
        'history + docs', 'infotainment', 'science', 'special interest',
        'true stories',
    ),
    'Drama': (
        'drama series', 'dramas', 'serie',
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
    'Supernatural Drama': (
        'supernatural', 'supernatural drama',
    ),
    'Toons': (
        'animated', 'animation', 'cartoon', 'cartoons', 'toons',
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
#   - Game Shows precedes Reality TV; History precedes Documentary.
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
    ('History',        ('history', 'histoire', 'historia')),
    ('Documentary',    ('doc', 'science', 'biograph', 'educat')),
    ('Drama',          ('drama', 'soap', 'telenovela')),
    ('Nature',         ('nature', 'wildlife', 'animal')),
    ('Faith',          ('faith', 'relig', 'gospel', 'church')),
    ('Westerns',       ('western',)),
    ('Classic TV',     ('classic', 'retro', 'vintage', 'nostalgi')),
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
    ('Drama',          ('drama', 'hallmark', 'law & order', 'csi', 'ncis')),
    ('Movies',         ('movies', 'movie', 'cinema', 'film', 'paramount movie')),
    ('Westerns',       ('western', 'gunsmoke', 'wild west', 'bonanza')),
    ('Food & Travel',  ('travel', 'journy', 'travelxp')),
    ('History',        ('history', 'historic', 'warfare')),
    ('Documentary',    ('smithsonian', 'science', 'mythbusters', 'nasa+', 'space')),
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
