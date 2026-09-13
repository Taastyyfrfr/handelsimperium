import json
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

from app.engine.notifications import create_notification
from app.engine.guilds import get_user_guild_membership


def ensure_active_auctions(db_session=None):
    """
    Idempotent Kontor Auction Initialization:
    Checks all regions in the regions table.
    If any region lacks an auction record with status 'ACTIVE',
    inserts an active auction with current_highest_bid = 0.0,
    epoch_end_at / end_time = NOW() + INTERVAL '7 days', and status 'ACTIVE'.
    Accepts a database connection, cursor, or None.
    """
    def _execute(cur):
        cur.execute(
            """
            INSERT INTO kontor_auctions (region_id, current_highest_bid, highest_bidder_guild_id, epoch_end_at, end_time, status)
            SELECT id, 0.0, NULL, NOW() + INTERVAL '7 days', NOW() + INTERVAL '7 days', 'ACTIVE'
            FROM regions r
            WHERE NOT EXISTS (
                SELECT 1 FROM kontor_auctions ka WHERE ka.region_id = r.id AND ka.status = 'ACTIVE'
            )
            """
        )

    if db_session is None:
        from app.database import get_db_connection
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                _execute(cur)
                conn.commit()
    elif hasattr(db_session, "cursor"):
        # Connection object
        with db_session.cursor() as cur:
            _execute(cur)
    else:
        # Cursor object
        _execute(db_session)


# Backward-compatibility alias
ensure_kontor_auctions = ensure_active_auctions


def resolve_kontor_auctions(cur) -> int:
    """
    Checks all ACTIVE auctions whose epoch has expired (epoch_end_at <= NOW()).
    Deterministically resolves them:
    - Transitions auction status to RESOLVED
    - If a winning bid was placed, assigns/updates regional_controllers for 7 days
    - Starts the next 7-day auction epoch for that region
    """
    cur.execute(
        """
        SELECT ka.id, ka.region_id, ka.current_highest_bid, ka.highest_bidder_guild_id, ka.epoch_end_at,
               r.name AS region_name, r.tag AS region_tag,
               g.name AS guild_name, g.tag AS guild_tag
        FROM kontor_auctions ka
        JOIN regions r ON r.id = ka.region_id
        LEFT JOIN guilds g ON g.id = ka.highest_bidder_guild_id
        WHERE ka.status = 'ACTIVE' AND ka.epoch_end_at <= NOW()
        FOR UPDATE OF ka
        """
    )
    expired = cur.fetchall()
    count = 0

    for row in expired:
        auction_id = row["id"]
        region_id = row["region_id"]
        winning_guild_id = row["highest_bidder_guild_id"]
        winning_bid = float(row["current_highest_bid"])

        # 1. Mark auction RESOLVED
        cur.execute("UPDATE kontor_auctions SET status = 'RESOLVED' WHERE id = %s", (auction_id,))

        # 2. If valid winning guild, assign regional control for 7 days
        if winning_guild_id and winning_bid > 0:
            cur.execute(
                """
                INSERT INTO regional_controllers (region_id, guild_id, winning_bid, valid_until, updated_at)
                VALUES (%s, %s, %s, NOW() + INTERVAL '7 days', NOW())
                ON CONFLICT (region_id) DO UPDATE SET
                    guild_id = EXCLUDED.guild_id,
                    winning_bid = EXCLUDED.winning_bid,
                    valid_until = EXCLUDED.valid_until,
                    updated_at = NOW()
                """,
                (region_id, winning_guild_id, winning_bid),
            )

            # Notify guild leader
            cur.execute("SELECT leader_id FROM guilds WHERE id = %s", (winning_guild_id,))
            ldr = cur.fetchone()
            if ldr:
                create_notification(
                    cur,
                    user_id=ldr["leader_id"],
                    event_type="AUCTION_WON",
                    payload={
                        "region_id": region_id,
                        "region_tag": row["region_tag"],
                        "region_name": row["region_name"],
                        "winning_bid": winning_bid,
                    },
                )

        # 3. Schedule next 7-day cycle
        cur.execute(
            """
            INSERT INTO kontor_auctions (region_id, current_highest_bid, highest_bidder_guild_id, epoch_end_at, end_time, status)
            VALUES (%s, 0.0, NULL, NOW() + INTERVAL '7 days', NOW() + INTERVAL '7 days', 'ACTIVE')
            """,
            (region_id,),
        )
        count += 1

    return count


def deposit_to_guild_bank(cur, user_id: int, amount: float) -> Dict[str, Any]:
    """
    Deposits Taler from merchant's balance into the guild bank (War Chest).
    Records entry in guild_contributions.
    """
    membership = get_user_guild_membership(cur, user_id)
    if not membership:
        raise ValueError("Ihr müsst Mitglied einer Gilde sein, um in die Gildenkasse einzuzahlen.")

    amount = round(float(amount), 2)
    if amount <= 0:
        raise ValueError("Einzahlungsbetrag muss größer als 0 sein.")

    guild_id = membership["guild_id"]

    # Verify and deduct user balance atomically
    cur.execute("SELECT balance FROM users WHERE id = %s FOR UPDATE", (user_id,))
    user_row = cur.fetchone()
    user_bal = float(user_row["balance"]) if user_row else 0.0
    if user_bal < amount:
        raise ValueError(
            f"Unzureichendes Taler-Guthaben! Erforderlich: {amount:.2f} Taler, Verfügbar: {user_bal:.2f} Taler."
        )

    amount_dec = Decimal(str(amount))
    cur.execute("UPDATE users SET balance = balance - %s WHERE id = %s", (amount_dec, user_id))

    # Lock and update guild bank
    cur.execute("SELECT balance FROM guild_bank WHERE guild_id = %s FOR UPDATE", (guild_id,))
    gb_row = cur.fetchone()
    cur.execute("UPDATE guild_bank SET balance = balance + %s WHERE guild_id = %s", (amount_dec, guild_id))
    new_bank_bal = round((float(gb_row["balance"]) if gb_row else 0.0) + amount, 2)

    # Record in guild_contributions
    cur.execute(
        """
        INSERT INTO guild_contributions (guild_id, user_id, contribution_type, resource_type, amount)
        VALUES (%s, %s, 'BANK_DEPOSIT', 'balance', %s)
        """,
        (guild_id, user_id, amount_dec),
    )

    create_notification(
        cur,
        user_id=user_id,
        event_type="GUILD_BANK_DEPOSIT",
        payload={
            "guild_id": guild_id,
            "guild_name": membership["guild_name"],
            "amount": amount,
            "new_balance": new_bank_bal,
        },
    )

    return {
        "guild_id": guild_id,
        "guild_name": membership["guild_name"],
        "amount": amount,
        "new_bank_balance": new_bank_bal,
    }


def place_kontor_auction_bid(cur, user_id: int, auction_id: int, bid_amount: float) -> Dict[str, Any]:
    """
    Submits a bid for a regional Kontor auction on behalf of the user's guild:
    - Verifies user has role LEADER or OFFICER in their guild.
    - Resolves any expired auctions first.
    - Validates bid_amount > current_highest_bid.
    - Deducts bid delta (or full bid) atomically from guild_bank.balance.
    - Atomically refunds the previously outbid guild's bank in full.
    - Updates kontor_auctions.
    """
    # 1. Resolve auctions first
    resolve_kontor_auctions(cur)

    # 2. Check guild membership & privileges
    membership = get_user_guild_membership(cur, user_id)
    if not membership:
        raise ValueError("Ihr müsst Mitglied einer Gilde sein, um an Kontor-Auktionen teilzunehmen.")

    if membership["role"] not in ("LEADER", "OFFICER"):
        raise ValueError("Nur Gildenleiter und Offiziere dürfen Gebote bei Kontor-Auktionen abgeben.")

    guild_id = membership["guild_id"]
    bid_amount = round(float(bid_amount), 2)
    if bid_amount <= 0:
        raise ValueError("Gebot muss größer als 0 Taler sein.")

    # 3. Lock auction row
    cur.execute(
        """
        SELECT ka.id, ka.region_id, ka.current_highest_bid, ka.highest_bidder_guild_id, ka.epoch_end_at, ka.status,
               r.name AS region_name, r.tag AS region_tag
        FROM kontor_auctions ka
        JOIN regions r ON r.id = ka.region_id
        WHERE ka.id = %s
        FOR UPDATE OF ka
        """,
        (auction_id,),
    )
    auction = cur.fetchone()
    if not auction:
        raise ValueError("Kontor-Auktion nicht gefunden.")

    now = datetime.now(timezone.utc)
    epoch_end = auction["epoch_end_at"]
    if epoch_end.tzinfo is None:
        epoch_end = epoch_end.replace(tzinfo=timezone.utc)

    if auction["status"] != "ACTIVE" or epoch_end <= now:
        resolve_kontor_auctions(cur)
        raise ValueError("Auktion ist bereits abgelaufen.")

    current_highest = float(auction["current_highest_bid"])
    prev_bidder_guild_id = auction["highest_bidder_guild_id"]

    if bid_amount <= current_highest:
        raise ValueError(
            f"Gebot muss höher als das aktuelle Höchstgebot von {current_highest:.2f} Taler sein."
        )

    # 4. Handle guild bank deductions & refunds
    bid_amount_dec = Decimal(str(bid_amount))
    current_highest_dec = Decimal(str(current_highest))

    if prev_bidder_guild_id == guild_id:
        # Same guild increasing its bid -> deduct only the increment
        delta = Decimal(str(round(bid_amount - current_highest, 2)))
        cur.execute("SELECT balance FROM guild_bank WHERE guild_id = %s FOR UPDATE", (guild_id,))
        gb = cur.fetchone()
        current_funds = float(gb["balance"]) if gb else 0.0
        if current_funds < float(delta):
            raise ValueError(
                f"Unzureichende Gildenkasse! Erforderlich zur Erhöhung: {float(delta):.2f} Taler, In der Kasse: {current_funds:.2f} Taler."
            )
        cur.execute("UPDATE guild_bank SET balance = balance - %s WHERE guild_id = %s", (delta, guild_id))
    else:
        # Outbidding a rival guild (or initial bid) -> deduct full bid_amount
        cur.execute("SELECT balance FROM guild_bank WHERE guild_id = %s FOR UPDATE", (guild_id,))
        gb = cur.fetchone()
        current_funds = float(gb["balance"]) if gb else 0.0
        if current_funds < bid_amount:
            raise ValueError(
                f"Unzureichende Gildenkasse! Erforderlich für Gebot: {bid_amount:.2f} Taler, In der Kasse: {current_funds:.2f} Taler."
            )
        cur.execute("UPDATE guild_bank SET balance = balance - %s WHERE guild_id = %s", (bid_amount_dec, guild_id))

        # Refund previous outbid guild if there was one
        if prev_bidder_guild_id and current_highest > 0:
            cur.execute(
                "UPDATE guild_bank SET balance = balance + %s WHERE guild_id = %s",
                (current_highest_dec, prev_bidder_guild_id),
            )
            # Notify previous guild leader
            cur.execute("SELECT leader_id, tag FROM guilds WHERE id = %s", (prev_bidder_guild_id,))
            prev_g = cur.fetchone()
            if prev_g:
                create_notification(
                    cur,
                    user_id=prev_g["leader_id"],
                    event_type="AUCTION_OUTBID",
                    payload={
                        "auction_id": auction_id,
                        "region_tag": auction["region_tag"],
                        "refund_amount": current_highest,
                        "outbid_by": membership["guild_tag"],
                        "new_bid": bid_amount,
                    },
                )

    # 5. Record contribution
    cur.execute(
        """
        INSERT INTO guild_contributions (guild_id, user_id, contribution_type, resource_type, amount)
        VALUES (%s, %s, 'AUCTION_BID', 'balance', %s)
        """,
        (guild_id, user_id, bid_amount_dec),
    )

    # 6. Update auction
    cur.execute(
        """
        UPDATE kontor_auctions
        SET current_highest_bid = %s, highest_bidder_guild_id = %s
        WHERE id = %s
        """,
        (bid_amount_dec, guild_id, auction_id),
    )

    return {
        "auction_id": auction_id,
        "region_id": auction["region_id"],
        "region_tag": auction["region_tag"],
        "region_name": auction["region_name"],
        "bid_amount": bid_amount,
        "guild_id": guild_id,
        "guild_tag": membership["guild_tag"],
    }


def get_kontor_auctions_overview(cur, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Returns full overview of all 4 Hanseatic regions:
    - Active auction data (current highest bid, highest bidder tag, time remaining until epoch close)
    - Regional controller data (controlling guild name/tag, reign valid_until countdown)
    - User bidding privileges (can_bid: true if LEADER or OFFICER)
    """
    # 1. Resolve expired auctions & ensure current ones exist
    resolve_kontor_auctions(cur)
    ensure_kontor_auctions(cur)

    # 2. Check user's guild role
    user_guild_id = None
    can_bid = False
    if user_id:
        membership = get_user_guild_membership(cur, user_id)
        if membership:
            user_guild_id = membership["guild_id"]
            can_bid = membership["role"] in ("LEADER", "OFFICER")

    now = datetime.now(timezone.utc)

    cur.execute(
        """
        SELECT 
            r.id AS region_id, r.name AS region_name, r.tag AS region_tag,
            r.coord_x, r.coord_y, r.description AS region_desc,
            -- Regional controller
            rc.guild_id AS controller_guild_id,
            cg.name AS controller_guild_name,
            cg.tag AS controller_guild_tag,
            rc.winning_bid AS controller_winning_bid,
            rc.valid_until AS controller_valid_until,
            -- Active auction
            ka.id AS auction_id,
            ka.current_highest_bid,
            ka.highest_bidder_guild_id,
            bg.name AS highest_bidder_name,
            bg.tag AS highest_bidder_tag,
            ka.epoch_end_at,
            ka.status AS auction_status
        FROM regions r
        LEFT JOIN regional_controllers rc ON rc.region_id = r.id AND rc.valid_until > NOW()
        LEFT JOIN guilds cg ON cg.id = rc.guild_id
        LEFT JOIN kontor_auctions ka ON ka.region_id = r.id AND ka.status = 'ACTIVE'
        LEFT JOIN guilds bg ON bg.id = ka.highest_bidder_guild_id
        ORDER BY r.id ASC
        """
    )
    rows = cur.fetchall()
    results = []

    for r in rows:
        # Controller reign countdown
        valid_until = r["controller_valid_until"]
        if valid_until:
            if valid_until.tzinfo is None:
                valid_until = valid_until.replace(tzinfo=timezone.utc)
            reign_rem_sec = max(0, int((valid_until - now).total_seconds()))
            reign_days = reign_rem_sec // 86400
            reign_hours = (reign_rem_sec % 86400) // 3600
            reign_str = f"{reign_days}d {reign_hours}h"
        else:
            reign_str = None

        # Auction countdown
        epoch_end = r["epoch_end_at"]
        if epoch_end:
            if epoch_end.tzinfo is None:
                epoch_end = epoch_end.replace(tzinfo=timezone.utc)
            auc_rem_sec = max(0, int((epoch_end - now).total_seconds()))
            auc_days = auc_rem_sec // 86400
            auc_hours = (auc_rem_sec % 86400) // 3600
            auc_mins = (auc_rem_sec % 3600) // 60
            if auc_days > 0:
                countdown_str = f"{auc_days}d {auc_hours:02d}h {auc_mins:02d}m"
            else:
                countdown_str = f"{auc_hours:02d}h {auc_mins:02d}m"
        else:
            countdown_str = "Beendet"

        cur_highest = float(r["current_highest_bid"] or 0.0)
        is_own_highest = (r["highest_bidder_guild_id"] == user_guild_id) if user_guild_id else False

        results.append({
            "region_id": r["region_id"],
            "region_name": r["region_name"],
            "region_tag": r["region_tag"],
            "region_desc": r["region_desc"],
            "controller_guild_id": r["controller_guild_id"],
            "controller_guild_name": r["controller_guild_name"],
            "controller_guild_tag": r["controller_guild_tag"],
            "controller_winning_bid": float(r["controller_winning_bid"] or 0.0),
            "reign_countdown": reign_str,
            "has_controller": r["controller_guild_id"] is not None,
            "auction_id": r["auction_id"],
            "current_highest_bid": cur_highest,
            "highest_bidder_guild_id": r["highest_bidder_guild_id"],
            "highest_bidder_name": r["highest_bidder_name"],
            "highest_bidder_tag": r["highest_bidder_tag"],
            "epoch_end_at": epoch_end,
            "auction_countdown": countdown_str,
            "can_bid": can_bid,
            "is_own_highest": is_own_highest,
            "min_next_bid": round(cur_highest + 50.0, 2) if cur_highest > 0 else 100.0,
        })

    return results


def get_user_travel_speed_multiplier(cur, user_id: int, origin_region_id: int, dest_region_id: int) -> float:
    """
    Checks if the user belongs to a guild that currently controls either the
    origin or destination region.
    If so, grants a 25% travel duration discount (duration multiplier: 0.75).
    """
    if not user_id:
        return 1.0

    cur.execute(
        """
        SELECT 1
        FROM guild_members gm
        JOIN regional_controllers rc ON rc.guild_id = gm.guild_id
        WHERE gm.user_id = %s 
          AND rc.region_id IN (%s, %s)
          AND rc.valid_until > NOW()
        LIMIT 1
        """,
        (user_id, origin_region_id, dest_region_id),
    )
    if cur.fetchone():
        return 0.75
    return 1.0


def credit_regional_trade_tax(cur, seller_id: int, trade_value: float):
    """
    Credits a 0.5% trade tax dividend to the controlling guild's bank for the
    region where the trade is settled (seller's Kontor region).
    Safeguarded against unassigned, null, or expired territorial controllers.
    """
    if trade_value <= 0:
        return

    cur.execute("SELECT region_id FROM users WHERE id = %s", (seller_id,))
    user_row = cur.fetchone()
    if not user_row or not user_row.get("region_id"):
        return

    seller_region = user_row["region_id"]
    tax_dividend = Decimal(str(round(trade_value * 0.005, 2)))
    if tax_dividend <= 0:
        return

    # Check for active regional controller
    cur.execute(
        """
        SELECT guild_id
        FROM regional_controllers
        WHERE region_id = %s
          AND guild_id IS NOT NULL
          AND valid_until > NOW()
        """,
        (seller_region,),
    )
    controller_row = cur.fetchone()
    if not controller_row or not controller_row.get("guild_id"):
        # No active controlling guild; dividend remains in standard market fee burn sink
        return

    controlling_guild_id = controller_row["guild_id"]
    cur.execute(
        """
        UPDATE guild_bank
        SET balance = balance + %s
        WHERE guild_id = %s
        """,
        (tax_dividend, controlling_guild_id),
    )
