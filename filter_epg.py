#!/usr/bin/env python3
"""
Filters each XMLTV source in epg_urls.txt down to the channels kept in
the M3U of the same name (output/<NAME>.m3u, produced by filter_m3u.py
from the matching NAME in urls.txt), and writes it to output/ as its own
.xml (not merged).

Strictly paired: an epg_urls.txt line "NAME = URL" is processed only if
output/NAME.m3u exists. Bare URLs, and names with no matching M3U, are
skipped without being downloaded.

Usage:
    python filter_epg.py            # every paired entry
    python filter_epg.py TV Plex    # only the named entries

Uses lxml.etree.iterparse + element clearing so large XMLTV files never
get fully loaded into memory.

epg_urls.txt format (one per line, blank/# lines ignored):
    TV   = https://example.com/epg.xml.gz
    Plex = https://example.com/plex.xml
"""
import gzip
import io
