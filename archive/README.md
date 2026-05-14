# Archive

Historical files no longer in active use but preserved for reference and audit.

## `symbol_mapping_ftmo_exness_v1.json`

Original symbol mapping file from Phase 1–3. Replaced in Phase 4.A by the
per-Exness-account cache architecture (see
`docs/phase-4-symbol-mapping-design.md`).

- The FTMO portion was extracted to `server/data/ftmo_whitelist.json` via
  `scripts/migrate_extract_ftmo_whitelist.py` (one-shot, run at step 4.A.1).
- The Exness portion is intentionally **not** auto-converted into the new
  cache format. The numerical specs in this file are static and may be
  outdated relative to the live broker — CEO re-creates each account's
  mapping via the Web UI wizard (step 4.A.6+) on first connect.

Read-only reference. Do not modify these files.
