from fastapi import APIRouter, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.auth import get_current_user
from app.database import get_db_connection
from app.config import SUPPORTED_RESOURCES
from app.engine.matching import place_and_match_order, cancel_order, get_order_book
from app.engine.production import calculate_offline_production
import os

router = APIRouter(prefix="/market", tags=["market"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))

@router.get("/book", response_class=HTMLResponse)
def market_book_view(
    request: Request,
    resource: str = "wood",
    user: dict = Depends(get_current_user),
):
    if resource not in SUPPORTED_RESOURCES:
        resource = "wood"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Ensure fresh user state
            calculate_offline_production(cur, user["id"])
            cur.execute("SELECT id, username, balance FROM users WHERE id = %s", (user["id"],))
            fresh_user = cur.fetchone()
            cur.execute("SELECT resource_type, amount FROM inventories WHERE user_id = %s", (user["id"],))
            inventories = {r["resource_type"]: float(r["amount"]) for r in cur.fetchall()}
            book_data = get_order_book(cur, resource, user["id"])
            conn.commit()

    return templates.TemplateResponse(
        request=request,
        name="components/market.html",
        context={
            "user": fresh_user,
            "current_resource": resource,
            "resources": SUPPORTED_RESOURCES,
            "user_inventory": inventories.get(resource, 0.0),
            "bids": book_data["bids"],
            "asks": book_data["asks"],
            "my_orders": book_data["my_orders"],
            "message": None,
            "error": None,
        },
    )

@router.post("/order", response_class=HTMLResponse)
def create_market_order(
    request: Request,
    order_type: str = Form(...),
    resource_type: str = Form(...),
    amount: float = Form(...),
    limit_price: float = Form(...),
    user: dict = Depends(get_current_user),
):
    message = None
    error = None

    if resource_type not in SUPPORTED_RESOURCES:
        resource_type = "wood"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                res = place_and_match_order(
                    cur,
                    user_id=user["id"],
                    order_type=order_type,
                    resource_type=resource_type,
                    amount=amount,
                    limit_price=limit_price,
                )
                conn.commit()
                if res["filled_amount"] >= res["initial_amount"]:
                    message = f"Order #{res['order_id']} ({order_type} {res['initial_amount']} {resource_type} @ {limit_price}) sofort vollständig ausgeführt!"
                elif res["filled_amount"] > 0:
                    message = f"Order #{res['order_id']} teilweise ausgeführt ({res['filled_amount']}/{res['initial_amount']}). Rest aktiv im Orderbuch."
                else:
                    message = f"Order #{res['order_id']} ({order_type} {res['initial_amount']} {resource_type} @ {limit_price}) erfolgreich im Orderbuch platziert."
            except ValueError as e:
                conn.rollback()
                error = str(e)
            
            # Fetch fresh state for rendering
            calculate_offline_production(cur, user["id"])
            cur.execute("SELECT id, username, balance FROM users WHERE id = %s", (user["id"],))
            fresh_user = cur.fetchone()
            cur.execute("SELECT resource_type, amount FROM inventories WHERE user_id = %s", (user["id"],))
            inventories = {r["resource_type"]: float(r["amount"]) for r in cur.fetchall()}
            book_data = get_order_book(cur, resource_type, user["id"])
            conn.commit()

    return templates.TemplateResponse(
        request=request,
        name="components/market.html",
        context={
            "user": fresh_user,
            "current_resource": resource_type,
            "resources": SUPPORTED_RESOURCES,
            "user_inventory": inventories.get(resource_type, 0.0),
            "bids": book_data["bids"],
            "asks": book_data["asks"],
            "my_orders": book_data["my_orders"],
            "message": message,
            "error": error,
        },
    )

@router.post("/orders/{order_id}/cancel", response_class=HTMLResponse)
@router.post("/cancel", response_class=HTMLResponse)
def cancel_market_order(
    request: Request,
    order_id: int,
    resource_type: str = Form("wood"),
    user: dict = Depends(get_current_user),
):
    """
    Cancels an active limit order and atomically refunds unfulfilled escrowed funds/goods.
    """
    message = None
    error = None

    if resource_type not in SUPPORTED_RESOURCES:
        resource_type = "wood"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                res = cancel_order(cur, user["id"], order_id)
                conn.commit()
                if res.get("refunded_funds", 0) > 0:
                    message = f"Kauf-Order #{order_id} storniert. {res['refunded_funds']:.2f} Taler Treuhandguthaben gutgeschrieben."
                else:
                    message = f"Verkaufs-Order #{order_id} storniert. {res['refunded_amount']:.2f} {res['resource_type']} dem Lager gutgeschrieben."
            except PermissionError as pe:
                conn.rollback()
                error = str(pe)
            except ValueError as ve:
                conn.rollback()
                error = str(ve)
            except Exception as e:
                conn.rollback()
                error = f"Fehler beim Stornieren: {str(e)}"

            calculate_offline_production(cur, user["id"])
            cur.execute("SELECT id, username, balance FROM users WHERE id = %s", (user["id"],))
            fresh_user = cur.fetchone()
            cur.execute("SELECT resource_type, amount FROM inventories WHERE user_id = %s", (user["id"],))
            inventories = {r["resource_type"]: float(r["amount"]) for r in cur.fetchall()}
            book_data = get_order_book(cur, resource_type, user["id"])
            conn.commit()

    return templates.TemplateResponse(
        request=request,
        name="components/market.html",
        context={
            "user": fresh_user,
            "current_resource": resource_type,
            "resources": SUPPORTED_RESOURCES,
            "user_inventory": inventories.get(resource_type, 0.0),
            "bids": book_data["bids"],
            "asks": book_data["asks"],
            "my_orders": book_data["my_orders"],
            "message": message,
            "error": error,
        },
    )
