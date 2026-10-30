# Channel names exempt from category exclusion (category_exclude_list.py).
# Matched case-insensitively and exactly (after trimming whitespace) against
# each channel's display name. The channel is still subject to
# exclude_list.py's name filter and to dedupe.
CATEGORY_EXCEPTION_LIST = [
    "Screams TV",
]
