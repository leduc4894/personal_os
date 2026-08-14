"""PostgreSQL source-version store adapter package.

This package will implement the core source publication contracts (version
commits, idempotency and citation lookups) over PostgreSQL. Until those
contracts land it intentionally exports no symbols; composition roots must not
import unfinished surface.
"""
