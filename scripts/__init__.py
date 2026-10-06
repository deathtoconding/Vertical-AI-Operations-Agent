"""Operational and release scripts (importable package).

``app/cli.py`` imports these as ``scripts.*``, so the directory is declared a package: without
it, mypy sees ``scripts/verify_release.py`` both as a top-level module and as a package member
and refuses to continue.
"""
