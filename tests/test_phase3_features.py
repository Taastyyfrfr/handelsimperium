import pytest
from datetime import datetime, timezone, timedelta
from pydantic import ValidationError
from app.database import get_db_connection
from app.config import settings, STARTER_CONFIG
from app.models import OrderCreate
from app.rate_limiter import limiter
from app.engine.production import calculate_offline_production, get_latest_catchup, dismiss_catchup, ensure_user_entities
from app.engine.matching import place_and_match_order, get_market_statistics
from app.scripts.seed_market import seed_market

def test_catchup_calculation_gains_and_caps(db_conn):
    """
    Verifies that offline production calculation computes exact deltas,
    properly identifies quantities lost due to warehouse storage caps,
    and logs an undismissed entry in user_catchups.
    """
    ts = int(datetime.now().timestamp() * 1000)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 200.00) RETURNING id",
            (f"catchup_user_{ts}", "hash"),
        )
        user_id = cur.fetchone()["id"]
        ensure_user_entities(cur, user_id)
        
        # Set initial inventory: wood at 800 units, calculated 1 hour ago
        # Rate for lumberjack is 0.25 / sec. In 3600s: 900 units generated.
        # 800 + 900 = 1700. Capped at 1000 (Level 1 warehouse).
        # Expected produced: 900, lost: 700, net: 200.
        past_time = datetime.now(timezone.utc) - timedelta(seconds=3600)
        cur.execute(
            """
            UPDATE inventories
            SET amount = 800.00, last_calculated_at = %s
            WHERE user_id = %s AND resource_type = 'wood'
            """,
            (past_time, user_id),
        )
        cur.execute("UPDATE users SET last_active_at = %s WHERE id = %s", (past_time, user_id))

        # Execute offline calculation
        prod_data = calculate_offline_production(cur, user_id, record_catchup=True)

        # Verify inventory updated to cap 1000
        wood_inv = next(i for i in prod_data["inventories"] if i["resource_type"] == "wood")
        assert wood_inv["amount"] == 1000.00

        # Verify catchup data returned
        catchup_summary = prod_data["catchup"]
        assert catchup_summary["offline_seconds"] >= 3590.0
        wood_catchup = catchup_summary["production"]["wood"]
        assert wood_catchup["produced"] == pytest.approx(900.0, abs=1.0)
        assert wood_catchup["lost"] == pytest.approx(700.0, abs=1.0)
        assert wood_catchup["net_added"] == pytest.approx(200.0, abs=1.0)

        # Verify persisted in user_catchups
        stored_catchup = get_latest_catchup(cur, user_id)
        assert stored_catchup is not None
        assert stored_catchup["production_delta"]["wood"]["lost"] == pytest.approx(700.0, abs=1.0)

        # Verify dismissal
        dismiss_catchup(cur, user_id)
        assert get_latest_catchup(cur, user_id) is None


def test_vwap_and_market_statistics_accuracy(db_conn):
    """
    Verifies the mathematical accuracy of 24-hour VWAP and Last Price,
    and ensures trades older than 24 hours are excluded from the VWAP.
    """
    ts = int(datetime.now().timestamp() * 1000)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 10000.00) RETURNING id",
            (f"trader_a_{ts}", "hash"),
        )
        u_a = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 10000.00) RETURNING id",
            (f"trader_b_{ts}", "hash"),
        )
        u_b = cur.fetchone()["id"]

        now = datetime.now(timezone.utc)
        test_res = f"test_metal_{ts}"
        # Trade 1: 10 units @ 4.00 Taler (Value = 40.00), 2 hours ago
        cur.execute(
            """
            INSERT INTO trades (buyer_id, seller_id, resource_type, amount, price, fee, executed_at)
            VALUES (%s, %s, %s, 10.00, 4.00, 0.80, %s)
            """,
            (u_a, u_b, test_res, now - timedelta(hours=2)),
        )

        # Trade 2: 30 units @ 6.00 Taler (Value = 180.00), 10 minutes ago
        cur.execute(
            """
            INSERT INTO trades (buyer_id, seller_id, resource_type, amount, price, fee, executed_at)
            VALUES (%s, %s, %s, 30.00, 6.00, 3.60, %s)
            """,
            (u_a, u_b, test_res, now - timedelta(minutes=10)),
        )

        # Trade 3: 100 units @ 50.00 Taler (Value = 5000.00), 28 hours ago (EXCLUDED from 24h VWAP)
        cur.execute(
            """
            INSERT INTO trades (buyer_id, seller_id, resource_type, amount, price, fee, executed_at)
            VALUES (%s, %s, %s, 100.00, 50.00, 100.00, %s)
            """,
            (u_a, u_b, test_res, now - timedelta(hours=28)),
        )

        stats = get_market_statistics(cur, test_res)


        # Expected 24h VWAP = (40.00 + 180.00) / (10.00 + 30.00) = 220.00 / 40.00 = 5.50
        assert stats["vwap_24h"] == 5.50
        assert stats["volume_24h"] == 40.00
        assert stats["trade_count_24h"] == 2
        assert stats["last_price"] == 6.00
        assert len(stats["recent_trades"]) == 3


def test_order_input_validation_hardening():
    """
    Verifies that OrderCreate strictly enforces positive integer quantities,
    positive limit prices capped at 1,000,000, and valid resource identifiers.
    """
    # Valid order
    valid = OrderCreate(
        order_type="BUY",
        resource_type="wood",
        amount=10,
        limit_price=5.50,
    )
    assert valid.amount == 10
    assert valid.limit_price == 5.50

    # Invalid: zero or negative amount
    with pytest.raises(ValidationError):
        OrderCreate(order_type="BUY", resource_type="wood", amount=0, limit_price=5.0)

    with pytest.raises(ValidationError):
        OrderCreate(order_type="BUY", resource_type="wood", amount=-5, limit_price=5.0)

    # Invalid: zero or negative limit_price
    with pytest.raises(ValidationError):
        OrderCreate(order_type="BUY", resource_type="wood", amount=10, limit_price=0.0)

    with pytest.raises(ValidationError):
        OrderCreate(order_type="BUY", resource_type="wood", amount=10, limit_price=-2.5)

    # Invalid: limit price above 1,000,000 cap
    with pytest.raises(ValidationError):
        OrderCreate(order_type="BUY", resource_type="wood", amount=10, limit_price=1_000_001.00)

    # Invalid: unsupported resource
    with pytest.raises(ValidationError):
        OrderCreate(order_type="BUY", resource_type="diamond", amount=10, limit_price=5.0)

    # Invalid: unsupported order type
    with pytest.raises(ValidationError):
        OrderCreate(order_type="HOLD", resource_type="wood", amount=10, limit_price=5.0)

def test_rate_limiter_sliding_window():
    """
    Verifies that the SlidingWindowRateLimiter permits requests within quota
    and blocks subsequent bursts exceeding max_requests within the window.
    """
    limiter.reset()

    class DummyClient:
        host = "192.168.1.50"

    class DummyRequest:
        client = DummyClient()
        headers = {}
        cookies = {"imperium_session": "test_token_abc_123"}

    req = DummyRequest()
    scope = "test_order"

    # Allow 3 requests in 5 seconds
    assert limiter.check(req, max_requests=3, window_seconds=5, scope=scope) is True
    assert limiter.check(req, max_requests=3, window_seconds=5, scope=scope) is True
    assert limiter.check(req, max_requests=3, window_seconds=5, scope=scope) is True
    # 4th request must be rejected
    assert limiter.check(req, max_requests=3, window_seconds=5, scope=scope) is False

def test_starter_allocation_defaults():
    """Verifies that starter allocation config aligns with balanced progression requirements."""
    assert STARTER_CONFIG["balance"] == 200.00
    assert STARTER_CONFIG["inventories"]["wood"] == 50.00
    assert STARTER_CONFIG["inventories"]["stone"] == 50.00
    assert STARTER_CONFIG["inventories"]["iron"] == 10.00

def test_seed_market_script_idempotence(db_conn):
    """
    Verifies that the market seeder CLI establishes baseline liquidity across
    all 5 commodities (10 orders total) and creates 0 duplicate orders on repeated run.
    """
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM market_orders")

    run1 = seed_market(silent=True)
    assert run1["orders_created"] == 10
    assert run1["orders_skipped"] == 0

    run2 = seed_market(silent=True)
    assert run2["orders_created"] == 0
    assert run2["orders_skipped"] == 10


