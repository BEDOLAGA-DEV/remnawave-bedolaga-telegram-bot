"""Regression tests for bugs already fixed in prior sessions.

Each test in this package corresponds to one specific bug. Tests should
pass on `main` (the fix is in place) and fail when the fix is reverted.
Use `git revert` (then re-restore) on the fixing commit to verify
locally before relying on a green run."""
