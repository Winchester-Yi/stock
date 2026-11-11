# Research evidence replay fixtures

These fixtures are copied from locally captured public-market research data, not
made-up returns or predictions. They test processing semantics, not forecast skill.
Do not treat them as current quotes, an exchange-certified feed or a trading result.

- `research_20261111_1350.json`: original two-stock snapshot (002241/002242),
  information cutoff 2026-11-11 13:50:07.834089 +08:00; aligned bars 13:45.
  Includes zero returns, masked cross-day metrics, hourly lag, unverified titles,
  missing sector mapping and no target match in a collected LHB list.
- `large_move_20261111_1045.json`: actual qualified 15-minute period for 000678,
  bar end 10:45, information cutoff 10:50:20 +08:00; +9.50391644908617% one-bar
  return. Large but finite valid returns must not be clipped or equated with a
  price-limit, bad provider data, corporate action or identifiable investor intent.

Tests explicitly mutate copies for malformed-number, time, stale and null cases.
No operator price-basis attestation has been fabricated for these fixtures.
