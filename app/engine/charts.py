from typing import Dict, List, Any, Optional, Tuple
from app.config import REFERENCE_PRICES


def generate_price_chart_svg(cur, resource_type: str) -> str:
    """
    Generates a server-rendered zero-dependency inline SVG polyline chart
    (viewBox="0 0 300 80") visualizing trailing 24h price trends, VWAP markers,
    and min/max corridors.
    Gracefully handles empty trade histories with a neutral baseline reference line.
    """
    ref_price = REFERENCE_PRICES.get(resource_type, 5.00)

    # Query trades executed in the trailing 24 hours
    cur.execute(
        """
        SELECT price, amount, executed_at
        FROM trades
        WHERE resource_type = %s AND executed_at >= NOW() - INTERVAL '24 HOURS'
        ORDER BY executed_at ASC, id ASC
        """,
        (resource_type,),
    )
    trades = cur.fetchall()

    if not trades:
        # Neutral baseline reference line when no trades exist in 24h
        return f"""<svg viewBox="0 0 300 80" class="w-full h-20 overflow-visible" xmlns="http://www.w3.org/2000/svg">
  <rect width="300" height="80" fill="#020617" rx="8"/>
  <line x1="20" y1="40" x2="280" y2="40" stroke="#334155" stroke-width="1.5" stroke-dasharray="4,4"/>
  <circle cx="150" cy="40" r="3" fill="#64748b"/>
  <text x="150" y="32" text-anchor="middle" fill="#94a3b8" font-size="9" font-family="ui-monospace, monospace">Basis-Referenz: {ref_price:.2f} Taler</text>
  <text x="150" y="56" text-anchor="middle" fill="#64748b" font-size="8" font-family="ui-sans-serif, system-ui">Keine Börsenabschlüsse in den letzten 24h</text>
</svg>"""

    # Extract prices and compute VWAP
    prices = [float(t["price"]) for t in trades]
    total_volume = sum(float(t["amount"]) for t in trades)
    total_val = sum(float(t["price"]) * float(t["amount"]) for t in trades)
    vwap = round(total_val / total_volume, 2) if total_volume > 0 else prices[-1]

    # Coordinate mapping across viewBox: width=300, height=80
    # Usable plot area: x in [25, 275] (width=250), y in [16, 64] (height=48)
    n = len(prices)
    if n == 1:
        # Duplicate single trade to show a horizontal trend line
        plot_pts = [(25.0, prices[0]), (275.0, prices[0])]
    else:
        plot_pts = []
        for i, p in enumerate(prices):
            x = 25.0 + (i * (250.0 / (n - 1)))
            plot_pts.append((x, p))

    raw_min = min(prices)
    raw_max = max(prices)
    if raw_min == raw_max:
        span = max(raw_min * 0.1, 0.5)
        min_p = raw_min - span
        max_p = raw_max + span
    else:
        pad = (raw_max - raw_min) * 0.08
        min_p = raw_min - pad
        max_p = raw_max + pad

    p_range = max(max_p - min_p, 0.01)

    # Calculate SVG (x, y) for each point
    svg_coords = []
    for x, p in plot_pts:
        y = 64.0 - ((p - min_p) / p_range * 48.0)
        svg_coords.append((round(x, 1), round(y, 1)))

    polyline_points = " ".join([f"{x},{y}" for x, y in svg_coords])
    first_x = svg_coords[0][0]
    last_x = svg_coords[-1][0]
    area_points = f"{first_x},72.0 {polyline_points} {last_x},72.0"

    # Color coding based on trajectory
    is_bullish = prices[-1] >= prices[0]
    trend_color = "#10b981" if is_bullish else "#f43f5e"
    grad_id = f"grad-{'bull' if is_bullish else 'bear'}-{resource_type}"

    # VWAP dashed horizontal line
    vwap_y = round(64.0 - ((vwap - min_p) / p_range * 48.0), 1)
    vwap_y = max(14.0, min(68.0, vwap_y))

    # Last price marker
    last_pt = svg_coords[-1]

    return f"""<svg viewBox="0 0 300 80" class="w-full h-20 overflow-visible" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="{grad_id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{trend_color}" stop-opacity="0.35"/>
      <stop offset="100%" stop-color="{trend_color}" stop-opacity="0.0"/>
    </linearGradient>
  </defs>
  <rect width="300" height="80" fill="#020617" rx="8"/>
  <line x1="20" y1="64" x2="280" y2="64" stroke="#1e293b" stroke-width="1"/>
  <line x1="20" y1="16" x2="280" y2="16" stroke="#1e293b" stroke-width="1"/>

  <!-- Area under curve -->
  <polygon points="{area_points}" fill="url(#{grad_id})"/>

  <!-- VWAP dashed reference marker -->
  <line x1="20" y1="{vwap_y}" x2="280" y2="{vwap_y}" stroke="#f59e0b" stroke-width="1" stroke-dasharray="3,3" stroke-opacity="0.75"/>
  <text x="278" y="{max(12.0, vwap_y - 2)}" text-anchor="end" fill="#f59e0b" font-size="8" font-family="ui-monospace, monospace">VWAP {vwap:.2f}T</text>

  <!-- Trend Polyline -->
  <polyline points="{polyline_points}" fill="none" stroke="{trend_color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>

  <!-- Last Trade Pulse Dot -->
  <circle cx="{last_pt[0]}" cy="{last_pt[1]}" r="3" fill="{trend_color}" stroke="#0f172a" stroke-width="1.5"/>

  <!-- Min / Max Indicators -->
  <text x="24" y="74" fill="#64748b" font-size="8" font-family="ui-monospace, monospace">Min: {raw_min:.2f}</text>
  <text x="24" y="12" fill="#64748b" font-size="8" font-family="ui-monospace, monospace">Max: {raw_max:.2f}</text>
  <text x="150" y="74" text-anchor="middle" fill="#94a3b8" font-size="8" font-family="ui-sans-serif, system-ui">Aktuell: {prices[-1]:.2f} T ({len(trades)} Trades)</text>
</svg>"""
