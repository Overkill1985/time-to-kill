"""Team-name normalization and curated cross-provider aliases.

ESPN is the canonical team registry (teams.espn_id). Other providers name teams
differently - measured 2026-09-26, 0 of 230 PropLine college football names
matched ESPN's displayName exactly, while 221 matched after ``normalize_team_name``
against ESPN's displayName / location / shortDisplayName, with no ambiguity.

The curated table covers the rest. Every entry was verified against the ESPN
team id in ESPN's own scoreboard data; add entries the same way, never by guess.
"""

from __future__ import annotations

import re
import unicodedata

from ttk.domain import Sport


def normalize_team_name(name: str) -> str:
    """Lowercase ASCII words. Trailing 'St.' is State; leading 'St.' is Saint
    ('Boise St.' -> 'boise state', 'St. John's' -> 'saint john s')."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    words = re.sub(r"[^a-z0-9 ]", " ", s).split()
    if words and words[-1] == "st":
        words[-1] = "state"
    if words and words[0] == "st":
        words[0] = "saint"
    return " ".join(words)


# normalized provider name -> ESPN team id
CURATED_ALIASES: dict[Sport, dict[str, str]] = {
    Sport.CFB: {
        "appalachian state": "2026",  # ESPN: App State Mountaineers
        "grambling state": "2755",  # Grambling Tigers
        "liu": "2341",  # Long Island University Sharks
        "louisiana monroe": "2433",  # UL Monroe Warhawks
        "miami fl": "2390",  # Miami Hurricanes (not Miami (OH), id 193)
        "nicholls state": "2447",  # Nicholls Colonels
        "tennessee martin": "2630",  # UT Martin Skyhawks
        "upenn": "219",  # Pennsylvania Quakers (not Penn State, id 213)
        "california golden": "25",  # California Golden Bears (PropLine truncates)
        "north carolina central": "2428",  # North Carolina Central Eagles
        "nc central": "2428",
        "east texas a and m": "2837",  # ESPN still lists Texas A&M-Commerce Lions
        "east texas a and m lions": "2837",
    },
    Sport.NBA: {
        "los angeles clippers": "12",  # ESPN: LA Clippers (not the Lakers)
    },
    Sport.NFL: {
        "nola saints": "18",  # New Orleans Saints
    },
}
