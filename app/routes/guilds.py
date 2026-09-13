import os
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user
from app.database import get_db_connection
from app.engine.guilds import (
    create_guild,
    join_guild,
    leave_guild,
    contribute_to_project,
    get_user_guild_details,
    list_all_guilds,
)
from app.engine.auctions import (
    deposit_to_guild_bank,
    place_kontor_auction_bid,
    get_kontor_auctions_overview,
)

router = APIRouter(prefix="/guilds", tags=["guilds"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))

def render_guild_response(request: Request, user_id: int, message: str = None, error: str = None) -> HTMLResponse:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, balance FROM users WHERE id = %s", (user_id,))
            user_row = cur.fetchone()

            cur.execute("SELECT resource_type, amount FROM inventories WHERE user_id = %s", (user_id,))
            inv_map = {r["resource_type"]: float(r["amount"]) for r in cur.fetchall()}

            guild_data = get_user_guild_details(cur, user_id)
            all_guilds = list_all_guilds(cur) if not guild_data else []
            kontor_auctions = get_kontor_auctions_overview(cur, user_id)

    return templates.TemplateResponse(
        request=request,
        name="components/guilds.html",
        context={
            "user": user_row,
            "guild_data": guild_data,
            "all_guilds": all_guilds,
            "kontor_auctions": kontor_auctions,
            "inv_map": inv_map,
            "message": message,
            "error": error,
        },
    )

@router.get("", response_class=HTMLResponse)
def get_guilds_view(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """
    Renders Guild Hall for affiliated players, or Recruitment & Founding view for unaffiliated players.
    """
    return render_guild_response(request, user["id"])

@router.post("/create", response_class=HTMLResponse)
def handle_create_guild(
    request: Request,
    name: str = Form(...),
    tag: str = Form(...),
    description: str = Form(""),
    user: dict = Depends(get_current_user),
):
    """
    Found a new guild (deducts 500 Taler fee, creates guild and initial monuments).
    """
    message = None
    error = None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                g = create_guild(cur, user["id"], name, tag, description)
                conn.commit()
                message = f"Gilde '{g['name']}' [{g['tag']}] erfolgreich gegründet! 500 Taler Gründungsgebühr entrichtet."
            except ValueError as e:
                conn.rollback()
                error = str(e)
            except Exception as e:
                conn.rollback()
                error = f"Fehler bei der Gildengründung: {str(e)}"
    return render_guild_response(request, user["id"], message=message, error=error)

@router.post("/{guild_id}/join", response_class=HTMLResponse)
def handle_join_guild(
    request: Request,
    guild_id: int,
    user: dict = Depends(get_current_user),
):
    """
    Joins an existing merchant guild.
    """
    message = None
    error = None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                res = join_guild(cur, user["id"], guild_id)
                conn.commit()
                message = f"Ihr seid der Gilde '{res['guild_name']}' [{res['tag']}] erfolgreich beigetreten!"
            except ValueError as e:
                conn.rollback()
                error = str(e)
            except Exception as e:
                conn.rollback()
                error = f"Fehler beim Gildenbeitritt: {str(e)}"
    return render_guild_response(request, user["id"], message=message, error=error)

@router.post("/leave", response_class=HTMLResponse)
def handle_leave_guild(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """
    Leaves the current guild. If the user is the leader, leadership transfers to the oldest standing member.
    """
    message = None
    error = None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                res = leave_guild(cur, user["id"])
                conn.commit()
                if res["disbanded"]:
                    message = f"Die Gilde '{res['guild_name']}' wurde aufgelöst, da keine weiteren Mitglieder verblieben waren."
                else:
                    message = f"Ihr habt die Gilde '{res['guild_name']}' verlassen."
            except ValueError as e:
                conn.rollback()
                error = str(e)
            except Exception as e:
                conn.rollback()
                error = f"Fehler beim Verlassen der Gilde: {str(e)}"
    return render_guild_response(request, user["id"], message=message, error=error)

@router.post("/projects/{project_id}/contribute", response_class=HTMLResponse)
def handle_contribute_project(
    request: Request,
    project_id: int,
    resource_type: str = Form(...),
    amount: float = Form(...),
    user: dict = Depends(get_current_user),
):
    """
    Contributes materials or Taler to a cooperative guild monument.
    """
    message = None
    error = None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                res = contribute_to_project(cur, user["id"], project_id, resource_type, amount)
                conn.commit()
                if res["is_completed"]:
                    message = f"🏆 Großartig! Mit Eurer Spende von {res['contributed_amount']:.1f} {res['resource_type']} wurde das Monument vollendet! Der Gilden-Buff ist nun aktiv."
                else:
                    message = f"Erfolgreich {res['contributed_amount']:.1f} {res['resource_type']} zum Baufortschritt beigesteuert!"
            except ValueError as e:
                conn.rollback()
                error = str(e)
            except Exception as e:
                conn.rollback()
                error = f"Fehler beim Spenden: {str(e)}"
    return render_guild_response(request, user["id"], message=message, error=error)

@router.post("/bank/deposit", response_class=HTMLResponse)
def handle_guild_bank_deposit(
    request: Request,
    amount: float = Form(...),
    user: dict = Depends(get_current_user),
):
    """
    Deposits Taler into the guild's collective treasury (War Chest).
    """
    message = None
    error = None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                res = deposit_to_guild_bank(cur, user["id"], amount)
                conn.commit()
                message = f"💰 {res['amount']:.2f} Taler erfolgreich in die Gildenkasse eingezahlt! Neuer Kassenbestand: {res['new_bank_balance']:.2f} Taler."
            except ValueError as e:
                conn.rollback()
                error = str(e)
            except Exception as e:
                conn.rollback()
                error = f"Fehler bei der Einzahlung: {str(e)}"
    return render_guild_response(request, user["id"], message=message, error=error)

@router.post("/auctions/{auction_id}/bid", response_class=HTMLResponse)
def handle_place_auction_bid(
    request: Request,
    auction_id: int,
    bid_amount: float = Form(...),
    user: dict = Depends(get_current_user),
):
    """
    Places a bid for regional Kontor control from the guild bank (LEADER/OFFICER only).
    """
    message = None
    error = None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                res = place_kontor_auction_bid(cur, user["id"], auction_id, bid_amount)
                conn.commit()
                message = f"👑 Gebot von {res['bid_amount']:.2f} Taler für das Kontor {res['region_name']} [{res['region_tag']}] erfolgreich abgegeben!"
            except ValueError as e:
                conn.rollback()
                error = str(e)
            except Exception as e:
                conn.rollback()
                error = f"Fehler bei der Gebotsabgabe: {str(e)}"
    return render_guild_response(request, user["id"], message=message, error=error)
