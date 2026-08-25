"""
Root-level PostgreSQL client singleton.

Import this in scripts and services:
    from postgres_client import pg
"""

from _postgres._postgres import Postgres

pg = Postgres()
