# Report startup measurements

Measured on 2026-10-28 using a read-only SQLite backup containing 1,194,447
records, 18,000 cached files and 5,503 prepared rollup rows. The original
database and agent-tool logs were not modified.

Each cell is the median of three separate Python processes. Imports, database
reads, aggregation and rendering to a terminal-like sink are included. Database
copying, live log discovery/parsing and exchange-rate network calls are excluded.
The baseline prints only the final report; the saved-report mode also prints
startup feedback and a matching, dated snapshot. The two modes were measured
sequentially on the same frozen corpus with normal system load variation.
These measurements exercise the snapshot path. The current UI takes that path
only with an unknown or day-old index; a recent index shows just the final
report and a transient status. The warm-cache rows describe cached records,
not a recent index timestamp, and do not benchmark that staleness gate.

| Scenario / metric | Final report only | With saved report |
| --- | ---: | ---: |
| Warm cache: first table | 1.081 s | 0.084 s |
| Warm cache: total | 1.485 s | 1.375 s |
| Warm cache: peak process memory | 217 MiB | 217 MiB |
| Rollup rebuild: first table | 9.747 s | 0.087 s |
| Rollup rebuild: total | 12.872 s | 13.314 s |
| Rollup rebuild: peak process memory | 1,961 MiB | 1,963 MiB |
| Simulated 5 s refresh: first table | 6.004 s | 0.089 s |
| Simulated 5 s refresh: total | 6.398 s | 6.424 s |
| Simulated 5 s refresh: peak process memory | 217 MiB | 218 MiB |

The early table is the saved report; it can be stale. The simulated refresh is
an injected 5 s delay, not a measurement of log parsing. The rebuild clears the
rollup fingerprint and performs an actual rebuild from cached records. This
remains expensive: the feature improves information during the wait, without
optimizing indexing or promising a faster completed report.

The rebuild total was 0.442 s (3.4%) above the baseline median. Three runs do
not isolate the cause from system variation. With no saved snapshot, a separate
run showed feedback after 81 ms, its first updated table after 0.960 s, and
finished in 1.353 s. Final report bodies matched byte for byte across the runs.

Costs and limits:

- One saved rendering, 66,312 bytes for this report, capped at 1 MiB and written
  with mode 0600. It contains local report data and preserves plain text.
- Terminal output grows from about 66 kB to 131 kB with the saved report.
  A slow terminal connection can make that more expensive than this local sink.
- A different selection, configuration, installed version or terminal width
  skips the snapshot. First-time users get startup feedback until indexing ends.
- No extra model calls or exchange-rate network requests are introduced.
- A separate 50-run microbenchmark measured about 9 ms median for 19 atomic
  diagnostic writes. This isolates bookkeeping rather than a complete report.
