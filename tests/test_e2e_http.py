import pytest
import httpx

BASE_URL = "http://localhost:80"

def test_full_http_htmx_flow(db_conn):
    import time
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM market_orders WHERE resource_type = 'wood'")

    ts = int(time.time() * 1000)
    user_a = f"kaufmann_a_{ts}"
    user_b = f"kaufmann_b_{ts}"


    with httpx.Client(base_url=BASE_URL, follow_redirects=True) as client_a:
        # 1. Register User A
        reg_a = client_a.post("/auth/register", data={
            "username": user_a,
            "password": "Password123!",
            "confirm_password": "Password123!",
        })
        assert reg_a.status_code == 200
        assert "Tor zur Handelsgilde" not in reg_a.text
        assert user_a in reg_a.text

        # 2. Get Resource Overview (HTMX partial)
        res_overview = client_a.get("/resources/overview")
        assert res_overview.status_code == 200
        assert "Lagerhaus & Produktion" in res_overview.text
        assert "Holzfällerhütte" in res_overview.text

        # 3. Upgrade Building
        up_res = client_a.post("/resources/upgrade", data={"building_type": "lumberjack"})
        assert up_res.status_code == 200
        assert "erfolgreich auf Stufe 2 ausgebaut" in up_res.text

        # 4. Get Order Book (HTMX partial)
        book_res = client_a.get("/market/book?resource=wood")
        assert book_res.status_code == 200
        assert "Orderbuch" in book_res.text

        # 5. Place BUY order (10 wood @ 2.50 Taler)
        order_buy = client_a.post("/market/order", data={
            "order_type": "BUY",
            "resource_type": "wood",
            "amount": 10.0,
            "limit_price": 2.50,
        })
        assert order_buy.status_code == 200
        assert "erfolgreich im Orderbuch platziert" in order_buy.text or "ausgeführt" in order_buy.text

    # User B registers and sells wood to match User A's order
    with httpx.Client(base_url=BASE_URL, follow_redirects=True) as client_b:
        reg_b = client_b.post("/auth/register", data={
            "username": user_b,
            "password": "Password123!",
            "confirm_password": "Password123!",
        })
        assert reg_b.status_code == 200

        # User B places SELL order (10 wood @ 2.50 Taler)
        order_sell = client_b.post("/market/order", data={
            "order_type": "SELL",
            "resource_type": "wood",
            "amount": 10.0,
            "limit_price": 2.50,
        })
        assert order_sell.status_code == 200

        assert "sofort vollständig ausgeführt" in order_sell.text or "Order #" in order_sell.text

        # 6. Check Transaction Log
        trades_res = client_b.get("/trades/log")
        assert trades_res.status_code == 200
        assert "Öffentliches Handelsprotokoll" in trades_res.text
        assert user_a in trades_res.text
        assert user_b in trades_res.text
        assert "Gebühr (2%)" in trades_res.text
