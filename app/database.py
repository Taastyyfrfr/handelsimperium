import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from contextlib import contextmanager
from app.config import settings

pool = ConnectionPool(
    conninfo=settings.conn_str,
    min_size=2,
    max_size=20,
    kwargs={"row_factory": dict_row},
    open=False,
)

def init_pool():
    global pool
    if pool._closed:
        pool = ConnectionPool(
            conninfo=settings.conn_str,
            min_size=2,
            max_size=20,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    elif not pool._opened:
        pool.open()

def close_pool():
    if pool._opened and not pool._closed:
        pool.close()


@contextmanager
def get_db_connection():
    """Yields a connection from the pool that auto-commits or rolls back on exception."""
    init_pool()
    with pool.connection() as conn:
        yield conn

@contextmanager
def get_db_cursor():
    """Yields a cursor within an active connection."""
    init_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            yield cur
