"""The ccreport server: one merged database over every machine that pushes.

The client cache at ~/.cache/ccreport/cache.db stays a per-machine artifact.
This package owns a second database, keyed by machine and account, that the
push client writes into and the read side aggregates over.

Nothing here may be imported from statusline.py, which renders on stdlib alone.
"""
