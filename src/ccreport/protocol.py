"""The version the push client and a ccreport server agree on over the wire.

One integer, bumped by hand, and separate from everything else that carries a
number. The package version comes off the newest `vX.Y.Z` tag through
hatch-vcs and moves with every release whether or not the bytes did, so it
cannot say whether a payload will be understood. Nor can a commit sha: every
commit differs from every other, which makes it advisory and nothing more.

Bump PROTOCOL_VERSION when, and only when, the bytes on the wire change:

- a new section on the ingest request or response (`samples`, `extra`, `pull`
  were three such changes, and `pull.tiers` a fourth)
- a field renamed, retyped, or given a new meaning
- a response key a client reads

A refactor that moves no bytes needs no bump. Neither does a schema change on
either side that the payload does not expose — `cache_db.MIGRATION_CHAIN` and
`server/db.py`'s chain are the versions for those, and they move on their own
terms.

Nothing here imports anything. `push.py` must stay clear of rich and FastAPI,
and `server/ingest.py` is on the other side of that line, so the one place they
can both reach has to cost neither of them an import.
"""

from __future__ import annotations

PROTOCOL_VERSION = 2
"""What this build speaks. See the bump rule above."""

PRE_VERSIONING = 0
"""What a peer that sends no version is speaking.

Every build before this constant existed. A client reads it off a server that
answered without a `protocol` key, and the server reads it off a batch that
carried none; either way it is lower than any real version, so the ordinary
comparison decides what happens.
"""


def describe(theirs: int, ours: int = PROTOCOL_VERSION) -> str:
    """One line on how *theirs* stands against *ours*, or "" when they match.

    The wording is the same wherever it is printed, so a person reading
    `ccreport server status` and a person reading a stopped push's reason are
    told the same thing.
    """
    if theirs == ours:
        return ""
    if theirs < ours:
        which = "pre-versioning" if theirs == PRE_VERSIONING else f"protocol {theirs}"
        return (
            f"the server speaks {which} and this machine speaks {ours}; "
            "what this build sends would be dropped without being stored"
        )
    return (
        f"the server speaks protocol {theirs} and this machine speaks {ours}; "
        "pushes still work, and updating this machine is what reads the rest"
    )
