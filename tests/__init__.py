"""Test suite package.

Declared as a package so tooling (mypy in particular) resolves ``tests.support.app`` by its
path instead of by basename — without this, ``tests/support/app.py`` is treated as a second
top-level module named ``app`` and shadows the application package.
"""
