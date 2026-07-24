---
paths:
  - "tests/**"
  - "**/test_*.py"
---
# Tests

- `tests/test_contract.py` validates the models against the canonical worked example read directly
  from `docs/CONTRACTS.md` sec 8. If you touch a schema, this is the first test to check.
- Every validated invariant in the contract should have a negative test that asserts the violation
  is rejected. When you add an invariant, add its rejection test alongside.
- Run with `uv run pytest -q`. The suite must pass on Python 3.12 (the contract's stated version).
