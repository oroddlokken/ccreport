"""Token usage and cost reporting for Claude Code.

Nothing is imported here on purpose. The status line imports this package on
every render and reaches straight for `statusline`; a re-export of `cache_db`
or `ccreport` would pull sqlite3, orjson and rich into that path, which is the
one cost the whole design is arranged to avoid.
"""
