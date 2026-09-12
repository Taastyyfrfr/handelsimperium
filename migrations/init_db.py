import os
import psycopg
from psycopg.rows import dict_row

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "handelsimperium_user")
DB_PASS = os.getenv("DB_PASS", "imperium_secret_2026")
DB_NAME = os.getenv("DB_NAME", "handelsimperium")

def init_database():
    print(f"Connecting to database {DB_NAME} as {DB_USER} to run migrations...")
    app_conn_str = f"host={DB_HOST} port={DB_PORT} user={DB_USER} password={DB_PASS} dbname={DB_NAME}"
    migration_file = os.path.join(os.path.dirname(__file__), "001_initial_schema.sql")
    with open(migration_file, "r") as f:
        sql_script = f.read()

    with psycopg.connect(app_conn_str, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_script)
            print("Migration 001_initial_schema.sql applied successfully!")

if __name__ == "__main__":
    init_database()
