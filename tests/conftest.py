import pytest
import os
import psycopg
from psycopg.rows import dict_row
from app.config import settings

@pytest.fixture(scope="session")
def db_conn():
    conn = psycopg.connect(settings.conn_str, autocommit=True, row_factory=dict_row)
    yield conn
    conn.close()
