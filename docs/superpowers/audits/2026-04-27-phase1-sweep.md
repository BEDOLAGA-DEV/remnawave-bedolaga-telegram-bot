# Phase 1 Static Sweep — 2026-04-27

Scope: cross-cutting bug-class grep across `app/`, `bedolaga-cabinet/src/`, `migrations/`.

## Findings

| # | Pattern | File | Line | Snippet | Severity | Decision | Action |
|---|---------|------|------|---------|----------|----------|--------|

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
