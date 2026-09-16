# Release validation

This document records the checks executed against the current source tree. It distinguishes automated verification from scientific assumptions and platform coverage.

## Automated checks

The release gate includes:

- the complete Python suite in `tests/v2`;
- frontend unit and interaction tests;
- TypeScript checking and the production browser build;
- Ruff, Black, dependency consistency, and Poetry lock validation;
- wheel and source-distribution construction;
- generated OpenAPI/TypeScript equality and canonical fixture checks;
- archive inventory and checksum verification for both example bundles.

For the 0.1.0 candidate, Python reported 289 passed and one sandbox-only loopback skip; the installed loopback workflow passed separately. Frontend testing reported 42 passed across nine files. TypeScript, production build, Ruff, Black, dependency checks, Poetry lock validation, and wheel/source-distribution construction passed. Tests requiring non-public Lausanne source data remain distinct from checks available in a clean source checkout.

## What the checks establish

Automated tests cover contract strictness, semantic random streams, event ordering, directed routing, supply/catalog stability, task outcomes, exposure conservation, portfolio sampling, nonlinear utility, statistics, job recovery, bounded queries, project portability, and browser interactions.

Passing tests do not calibrate synthetic operating assumptions, prove global optimization, guarantee public Overpass availability, or establish city-scale performance on untested hardware.

## Release assets

Release assets must come from the same source candidate and include SHA-256 checksums for the wheel, source distribution, and both example manifest/archive pairs. A clean installation must start without Node.js from the wheel, import both examples into an empty workspace, open retained results, duplicate an example, and complete a bounded new run and portfolio analysis.
