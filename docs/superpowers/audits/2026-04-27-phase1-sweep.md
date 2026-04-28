# Phase 1 Static Sweep — 2026-04-27

Scope: cross-cutting bug-class grep across `app/`, `bedolaga-cabinet/src/`, `migrations/`.

## Findings

| # | Pattern | File | Line | Snippet | Severity | Decision | Action |
|---|---------|------|------|---------|----------|----------|--------|
| 1 | P1 | app/database/database.py | 474 | `text(f'SELECT COALESCE(MAX({q_col}), 0) FROM {q_schema}.{q_table}')` — identifiers from information_schema, quoted via `_quote_ident` | info | accept-with-rationale | accept |
| 2 | P1 | app/database/database.py | 492 | `text(f'SELECT last_value, is_called FROM {q_seq_schema}.{q_seq_name}')` — sequence name parsed from `pg_get_serial_sequence`, quoted via `_quote_ident` | info | accept-with-rationale | accept |
| 3 | P1 | app/services/backup_service.py | 455 | `text(f'SELECT COUNT(*) FROM {table_name}')` — table_name from SQLAlchemy `inspect().get_table_names()` (schema reflection, not user input) | info | accept-with-rationale | accept |
| 4 | P1 | app/services/backup_service.py | 1569 | `text(f'TRUNCATE {tables_str} RESTART IDENTITY CASCADE')` — tables_str joined from hardcoded `all_tables` literal list (line 1435) | info | accept-with-rationale | accept |
| 5 | P1 | app/services/backup_service.py | 1579 | `text(f'TRUNCATE {table_name} CASCADE')` — table_name iterated over the same hardcoded `all_tables` list | info | accept-with-rationale | accept |
| 6 | P2 | app/ | - | No hits — pattern clean (previous fix in `app/handlers/admin/achievements.py::CONDITION_TYPES` holds) | info | accept-with-rationale | accept |

(Severity: critical / high / medium / low / info.
 Decision: real-bug / false-positive / accept-with-rationale.
 Action: quick-fix-applied / queue-phase2 / accept.)

## Pattern catalogue

- P1: Raw SQL injection vectors
- P2: Surrogate-pair string escapes
- P3: Swallowed exceptions
- P4: Code execution sinks
- P5: Hardcoded secrets
- P6: Missing auth on admin endpoints
- P7: Money-path race conditions
- P8: dangerouslySetInnerHTML without sanitiser
- P9: eval / new Function / sensitive localStorage
- P10: target="_blank" without rel="noopener noreferrer"
- P11: SQL migration anti-patterns

## Summary

(filled at end of Phase 1)
- Total hits: TBD-fill
- Real bugs (quick-fixed): TBD-fill
- Real bugs (queued for Phase 2): TBD-fill
- False positives: TBD-fill
- Accepted with rationale: TBD-fill
