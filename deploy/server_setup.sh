#!/usr/bin/env bash
set -e

echo "=== Configuring PostgreSQL ==="
sudo -u postgres psql << 'EOSQL'
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'handelsimperium_user') THEN
        CREATE ROLE handelsimperium_user WITH LOGIN PASSWORD 'imperium_secret_2026';
    ELSE
        ALTER ROLE handelsimperium_user WITH PASSWORD 'imperium_secret_2026';
    END IF;
END $$;
EOSQL

sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname = 'handelsimperium'" | grep -q 1 || sudo -u postgres psql -c "CREATE DATABASE handelsimperium OWNER handelsimperium_user;"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE handelsimperium TO handelsimperium_user;"
sudo -u postgres psql -d handelsimperium -c "GRANT ALL ON SCHEMA public TO handelsimperium_user;"

echo "=== PostgreSQL configured successfully ==="
