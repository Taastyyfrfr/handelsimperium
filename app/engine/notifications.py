from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
import json

def create_notification(cur, user_id: int, event_type: str, payload: Dict[str, Any]):
    """
    Inserts a structured notification event for a merchant.
    Event types:
      - 'TRADE_EXECUTED'
      - 'CONTRACT_FULFILLED'
      - 'STORAGE_OVERFLOW'
    """
    cur.execute(
        """
        INSERT INTO notifications (user_id, event_type, payload, is_read, created_at)
        VALUES (%s, %s, %s, FALSE, NOW())
        RETURNING id
        """,
        (user_id, event_type, json.dumps(payload)),
    )
    return cur.fetchone()["id"]

def get_user_notifications(cur, user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Retrieves recent notifications for the authenticated merchant.
    """
    cur.execute(
        """
        SELECT id, event_type, payload, is_read, created_at
        FROM notifications
        WHERE user_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        (user_id, limit),
    )
    rows = cur.fetchall()
    results = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        results.append({
            "id": r["id"],
            "event_type": r["event_type"],
            "payload": payload,
            "is_read": bool(r["is_read"]),
            "created_at": r["created_at"],
        })
    return results

def get_unread_notification_count(cur, user_id: int) -> int:
    """
    Counts unread notifications for a merchant.
    """
    cur.execute(
        "SELECT COUNT(*) AS count FROM notifications WHERE user_id = %s AND is_read = FALSE",
        (user_id,),
    )
    row = cur.fetchone()
    return row["count"] if row else 0

def mark_all_notifications_read(cur, user_id: int):
    """
    Marks all notifications as read for a merchant.
    """
    cur.execute(
        "UPDATE notifications SET is_read = TRUE WHERE user_id = %s AND is_read = FALSE",
        (user_id,),
    )
