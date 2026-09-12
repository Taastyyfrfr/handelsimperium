import secrets
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import JSONResponse

from app.config import settings, SUPPORTED_RESOURCES
from app.database import get_db_connection

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBasic()

def verify_admin_credentials(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """
    Validates administrative HTTP Basic Auth credentials.
    """
    is_user_ok = secrets.compare_digest(credentials.username, settings.ADMIN_USER)
    is_pass_ok = secrets.compare_digest(credentials.password, settings.ADMIN_PASS)
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ungültige Administrator-Zugangsdaten.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

@router.get("/economy", response_class=JSONResponse)
def get_economic_telemetry(
    request: Request,
    admin_user: str = Depends(verify_admin_credentials),
):
    """
    Macro-economic telemetry endpoint aggregating money supply, commodity reserves,
    24h trade volumes, and deflationary fee/contract sinks.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # 1. Total Currency Supply (Circulating + Escrowed)
            cur.execute("SELECT COALESCE(SUM(balance), 0) AS circulating FROM users")
            circulating = float(cur.fetchone()["circulating"])

            cur.execute(
                """
                SELECT COALESCE(SUM((amount - filled_amount) * limit_price), 0) AS escrowed
                FROM market_orders
                WHERE order_type = 'BUY' AND status = 'ACTIVE'
                """
            )
            escrowed_cash = float(cur.fetchone()["escrowed"])
            total_currency = round(circulating + escrowed_cash, 2)

            # 2. Total Commodity Reserves (Warehouses + Escrowed Sell Orders)
            cur.execute(
                """
                SELECT resource_type, COALESCE(SUM(amount), 0) AS amount
                FROM inventories
                GROUP BY resource_type
                """
            )
            free_inv = {r["resource_type"]: float(r["amount"]) for r in cur.fetchall()}

            cur.execute(
                """
                SELECT resource_type, COALESCE(SUM(amount - filled_amount), 0) AS amount
                FROM market_orders
                WHERE order_type = 'SELL' AND status = 'ACTIVE'
                GROUP BY resource_type
                """
            )
            escrowed_inv = {r["resource_type"]: float(r["amount"]) for r in cur.fetchall()}

            commodity_reserves = {}
            total_commodity_units = 0.0
            for res in SUPPORTED_RESOURCES:
                free = free_inv.get(res, 0.0)
                escrow = escrowed_inv.get(res, 0.0)
                tot = round(free + escrow, 2)
                commodity_reserves[res] = {
                    "free_warehouse_stock": free,
                    "escrowed_sell_orders": escrow,
                    "total_stock": tot,
                }
                total_commodity_units += tot
            total_commodity_units = round(total_commodity_units, 2)

            # 3. 24-Hour Market Activity & Fee Burn Sink
            cur.execute(
                """
                SELECT COUNT(*) AS count,
                       COALESCE(SUM(amount), 0) AS units,
                       COALESCE(SUM(amount * price), 0) AS taler,
                       COALESCE(SUM(fee), 0) AS fees
                FROM trades
                WHERE executed_at >= NOW() - INTERVAL '24 hours'
                """
            )
            trade_24h = cur.fetchone()

            cur.execute("SELECT COALESCE(SUM(fee), 0) AS lifetime_fees FROM trades")
            lifetime_fees = float(cur.fetchone()["lifetime_fees"])

            # 4. Export Contracts Sink (Permanently Burned Commodities)
            cur.execute(
                """
                SELECT COUNT(*) AS count,
                       COALESCE(SUM(reward_taler), 0) AS paid,
                       COALESCE(SUM(target_amount), 0) AS burned
                FROM export_contracts
                WHERE status = 'FULFILLED'
                """
            )
            contracts_data = cur.fetchone()

            # 5. Accounts Count
            cur.execute("SELECT COUNT(*) AS total_accounts FROM users")
            total_accounts = int(cur.fetchone()["total_accounts"])

    return {
        "status": "success",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "money_supply": {
            "circulating_taler": round(circulating, 2),
            "escrowed_buyer_taler": round(escrowed_cash, 2),
            "total_currency_supply": total_currency,
        },
        "commodity_reserves": {
            "breakdown": commodity_reserves,
            "total_units_in_circulation": total_commodity_units,
        },
        "market_activity_24h": {
            "trade_count": int(trade_24h["count"]),
            "volume_units": round(float(trade_24h["units"]), 2),
            "volume_taler": round(float(trade_24h["taler"]), 2),
            "burned_market_fees": round(float(trade_24h["fees"]), 2),
        },
        "system_sinks": {
            "lifetime_burned_market_fees": round(lifetime_fees, 2),
            "lifetime_export_contracts_fulfilled": int(contracts_data["count"]),
            "lifetime_export_taler_injected": round(float(contracts_data["paid"]), 2),
            "lifetime_export_commodities_absorbed": round(float(contracts_data["burned"]), 2),
        },
        "merchants": {
            "total_registered_accounts": total_accounts,
        },
    }
