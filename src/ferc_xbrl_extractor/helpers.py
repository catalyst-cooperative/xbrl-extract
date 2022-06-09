"""Helper functions."""
import logging

import sqlalchemy as sa


def drop_tables(engine: sa.engine.Engine):
    """
    Drops all tables from a SQLite database.

    Creates an sa.schema.MetaData object reflecting the structure of the
    database that the passed in ``engine`` refers to, and uses that schema to
    drop all existing tables.

    Todo:
        Treat DB connection as a context manager (with/as).

    Args:
        engine (sa.engine.Engine): An SQL Alchemy SQLite database Engine
            pointing at an exising SQLite database to be deleted.

    Returns:
        None

    """
    logger = logging.getLogger(__name__)
    logger.info("Dropping tables")

    md = sa.MetaData()
    md.reflect(engine)
    md.drop_all(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("VACUUM")


def get_logger(name: str):
    """Helper function to append 'catalystcoop' to logger name and return logger."""
    return logging.getLogger(f"catalystcoop.{name}")
