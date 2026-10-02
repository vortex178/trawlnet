"""The record each board module declares as BOARD; `sources` builds its dispatch tables from them."""
from __future__ import annotations

import re
from typing import Callable, NamedTuple


class Board(NamedTuple):
    """`name` is the records' "source" (and companies[].ats for token boards). A careers board (`careers` set) emits
    source "custom", so its name is only a label and it uses `resolve`, not `describe`. `undocumented` gates fetch_all
    only: `describe` runs on the board's own records so it is covered, `resolve` must check the opt-in itself (as
    atlassian_description does), and `link` belongs on documented boards only."""
    name: str
    fetch: Callable | None = None      # company entry -> records
    describe: Callable | None = None   # detail_url -> description, when the list endpoint has none (shortlist only)
    link: str | None = None            # regex on a linked job URL; group 1 is the board token (HN/Remote OK enrichment)
    resolve: Callable | None = None    # career-site job URL -> description, "" when the URL is not this board's
    careers: re.Pattern | None = None  # custom careers_url served by `fetch` instead of the generic scraper
    undocumented: bool = False         # undocumented endpoint: fetched only with sources.undocumented_ats
