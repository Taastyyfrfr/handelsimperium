from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.auth import get_current_user
from app.database import get_db_connection
import os

router = APIRouter(prefix="/trades", tags=["trades"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))

@router.get("/log", response_class=HTMLResponse)
def transaction_log_view(request: Request, user: dict = Depends(get_current_user)):
    """
    HTMX-driven Transaction Log view.
    Displays global trade executions, market fees collected, and highlights user trades.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 
                    t.id,
                    t.resource_type,
                    t.amount,
                    t.price,
                    t.fee,
                    t.executed_at,
                    b.username AS buyer_name,
                    s.username AS seller_name,
                    t.buyer_id,
                    t.seller_id
                FROM trades t
                JOIN users b ON t.buyer_id = b.id
                JOIN users s ON t.seller_id = s.id
                ORDER BY t.executed_at DESC
                LIMIT 50
                """
            )
            raw_trades = cur.fetchall()
            
            cur.execute("SELECT COUNT(*), COALESCE(SUM(fee), 0) AS total_fee, COALESCE(SUM(amount * price), 0) AS total_volume FROM trades")
            stats = cur.fetchone()

    trade_items = []
    for r in raw_trades:
        val = round(float(r["amount"]) * float(r["price"]), 2)
        trade_items.append({
            "id": r["id"],
            "resource_type": r["resource_type"],
            "amount": float(r["amount"]),
            "price": float(r["price"]),
            "fee": float(r["fee"]),
            "total_value": val,
            "buyer_name": r["buyer_name"],
            "seller_name": r["seller_name"],
            "is_buyer": r["buyer_id"] == user["id"],
            "is_seller": r["seller_id"] == user["id"],
            "executed_at": r["executed_at"].strftime("%H:%M:%S (%d.%m)"),
        })

    return templates.TemplateResponse(
        request=request,
        name="components/trades.html",
        context={
            "user": user,
            "trades": trade_items,
            "total_trades": stats["count"] if stats else 0,
            "total_volume": round(float(stats["total_volume"] if stats else 0), 2),
            "total_fee": round(float(stats["total_fee"] if stats else 0), 2),
        },
    )
