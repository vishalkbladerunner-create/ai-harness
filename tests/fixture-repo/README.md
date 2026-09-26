# Fixture repo

A tiny Python repo with an off-by-one bug.

- `buggy.py:last_item` uses `items[len(items)]` instead of `items[len(items) - 1]`.
- `test_buggy.py` exercises the bug.

Run tests with `pytest`.
