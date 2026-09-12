import pytest
from datetime import datetime, timezone, timedelta
from app.engine.production import (
    calculate_offline_production,
    ensure_user_entities,
    get_storage_cap,
    upgrade_building,
)
from app.auth import hash_password
from app.config import BUILDING_CONFIG

def test_offline_production_delta(db_conn):
    with db_conn.cursor() as cur:
        # Create test user
        username = f"test_farmer_{datetime.now().timestamp()}"
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 1000.00) RETURNING id",
            (username, hash_password("secret123")),
        )
        user_id = cur.fetchone()["id"]
        ensure_user_entities(cur, user_id)

        # Set initial amount to 50 and last_calculated_at to 2 hours ago
        two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
        cur.execute(
            "UPDATE inventories SET amount = 50.00, last_calculated_at = %s WHERE user_id = %s AND resource_type = 'wood'",
            (two_hours_ago, user_id),
        )

        # Run production calculation
        data = calculate_offline_production(cur, user_id)
        
        # Check wood inventory
        wood_inv = next(i for i in data["inventories"] if i["resource_type"] == "wood")
        rate = BUILDING_CONFIG["lumberjack"]["base_rate"]
        expected_min_generated = 7200 * rate  # 2 hours in seconds * rate
        storage_cap = get_storage_cap(1)
        expected_amount = min(storage_cap, 50.00 + expected_min_generated)

        assert wood_inv["amount"] == pytest.approx(expected_amount, abs=2.0)
        assert wood_inv["delta_seconds"] >= 7190.0

def test_storage_cap_enforcement(db_conn):
    with db_conn.cursor() as cur:
        username = f"test_cap_{datetime.now().timestamp()}"
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 1000.00) RETURNING id",
            (username, hash_password("secret123")),
        )
        user_id = cur.fetchone()["id"]
        ensure_user_entities(cur, user_id)

        # Simulate 30 days offline
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        cur.execute(
            "UPDATE inventories SET amount = 100.00, last_calculated_at = %s WHERE user_id = %s AND resource_type = 'wood'",
            (thirty_days_ago, user_id),
        )

        data = calculate_offline_production(cur, user_id)
        wood_inv = next(i for i in data["inventories"] if i["resource_type"] == "wood")
        storage_cap = get_storage_cap(1)

        # Must not exceed storage cap
        assert wood_inv["amount"] == storage_cap
        assert wood_inv["amount"] <= 1500.0
