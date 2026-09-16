#!/usr/bin/env python3
"""
Kalshi <-> Polymarket cross-platform arbitrage PAPER SCANNER.

WHAT THIS DOES
---------------
Every time it runs, this script:
  1. Pulls public, no-auth market data from Kalshi and Polymarket.
  2. Fuzzy-matches markets that appear to describe the same real-world event.
  3. Computes the fee-adjusted "combined cost" of buying the opposite sides
     of the same bet on the two platforms (Kalshi YES + Polymarket NO, or
     Kalshi NO + Polymarket YES).
  4. If that combined cost is below $1.00 by more than a safety margin,
     logs it to a local SQLite database as a hypothetical (paper) trade.
  5. Prints a short summary.

WHAT THIS DOES NOT DO
----------------------
- It never places an order, never touches an API key, never moves money.
- It does not guarantee the "opportunities" it finds are real. Title-based
  matching produces false positives: two markets can sound identical while
  settling on different rules, different dates, or different source data.
  ALWAYS manually verify a flagged pair before considering real capital.
- Fee constants below are approximations of publicly documented formulas
  as of when this was written. Both platforms have changed fee schedules
  during 2026. Re-check https://kalshi.com and https://docs.polymarket.com
  before trusting any number this script prints.

WHY PAPER MODE FIRST
---------------------
Research on cross-platform prediction-market arbitrage in 2026 consistently
finds that gross spreads compress to thin (often sub-2%) net margins after
fees, and that competitive windows can close in single-digit seconds —
which a scanner running every few minutes on a free GitHub Actions cron
job will usually miss. Paper mode exists to answer one question honestly:
does a persistent, non-cherry-picked log show real, repeatable, fee-adjusted
edge over weeks of data, or does it mostly show noise and stale quotes?
Decide whether to risk real money only after you have that log.
"""

import json
import math
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Optional

import requests

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"

DB_PATH = "scanner_log.db"
REQUEST_TIMEOUT = 15
MAX_KALSHI_PAGES = 5       # ~200 markets/page -> up to ~1000 markets scanned
POLY_LIMIT = 500           # gamma API max per request

# ---------------------------------------------------------------------------
# Fee & risk assumptions — APPROXIMATE. Verify before relying on them.
# ---------------------------------------------------------------------------

def kalshi_taker_fee_cents(price_cents: float) -> float:
    """
    Kalshi's publicly documented taker-fee formula is approximately:
        fee = ceil(0.07 * contracts * price * (1 - price))
    for a single contract (contracts=1), price in dollars (0-1).
    This is a widely cited approximation, not a guarantee of the current
    rate — Kalshi's fee schedule varies by product and has changed before.
    Confirm at https://kalshi.com/docs/fees.
    """
    p = price_cents / 100.0
    fee_dollars = math.ceil(100 * 0.07 * p * (1 - p)) / 100.0
    return fee_dollars * 100  # back to cents

POLYMARKET_TAKER_FEE_CENTS = 0.0
# Polymarket's own docs describe fees as protocol-set and embedded per
# market rather than a flat published rate. Treated as 0 here; the safety
# margin below is meant to absorb this uncertainty. Verify at
# https://docs.polymarket.com before trusting this.

SAFETY_MARGIN_CENTS = 3.0   # cushion for slippage, spread-crossing, stale quotes
MIN_NET_EDGE_CENTS = 1.0    # ignore anything smaller than this after costs
TITLE_MATCH_THRESHOLD = 0.62  # difflib ratio; tune based on false-positive rate you observe


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_kalshi_markets() -> list[dict]:
    markets = []
    cursor = None
    for _ in range(MAX_KALSHI_PAGES):
        params = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = requests.get(f"{KALSHI_BASE}/markets", params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[kalshi] request failed: {e}", file=sys.stderr)
            break
        data = resp.json()
        batch = data.get("markets", [])
        markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
        time.sleep(0.2)  # be polite to the public endpoint
    return markets


def fetch_polymarket_markets() -> list[dict]:
    try:
        resp = requests.get(
            f"{GAMMA_BASE}/markets",
            params={"active": "true", "closed": "false", "limit": POLY_LIMIT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"[polymarket] request failed: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

_STOPWORDS = {"will", "the", "a", "an", "in", "on", "of", "to", "be", "by", "for", "at"}

def normalize_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    words = [w for w in title.split() if w not in _STOPWORDS]
    return " ".join(words)


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def find_candidate_matches(kalshi_markets: list[dict], poly_markets: list[dict]) -> list[tuple]:
    """
    Best-effort fuzzy match. This WILL produce false positives — two
    differently-settling markets can have very similar titles. Treat
    every match as a lead to manually verify, not a confirmed pair.
    """
    candidates = []
    for km in kalshi_markets:
        k_title = km.get("title") or km.get("subtitle") or ""
        if not k_title:
            continue
        for pm in poly_markets:
            p_title = pm.get("question") or ""
            if not p_title:
                continue
            score = title_similarity(k_title, p_title)
            if score >= TITLE_MATCH_THRESHOLD:
                candidates.append((score, km, pm))
    candidates.sort(key=lambda x: -x[0])
    return candidates


# ---------------------------------------------------------------------------
# Edge calculation
# ---------------------------------------------------------------------------

@dataclass
class Opportunity:
    timestamp: str
    match_score: float
    kalshi_ticker: str
    kalshi_title: str
    kalshi_close_time: Optional[str]
    poly_question: str
    poly_slug: Optional[str]
    poly_end_date: Optional[str]
    direction: str
    kalshi_leg_price_cents: float
    poly_leg_price_cents: float
    kalshi_fee_cents: float
    poly_fee_cents: float
    combined_cost_cents: float
    net_edge_cents: float


def parse_poly_prices(pm: dict) -> Optional[tuple]:
    try:
        outcomes = json.loads(pm.get("outcomes", "[]"))
        prices = json.loads(pm.get("outcomePrices", "[]"))
        idx_yes = outcomes.index("Yes")
        idx_no = outcomes.index("No")
        yes_price_cents = float(prices[idx_yes]) * 100
        no_price_cents = float(prices[idx_no]) * 100
        return yes_price_cents, no_price_cents
    except (ValueError, KeyError, json.JSONDecodeError, IndexError, TypeError):
        return None


def evaluate_pair(score: float, km: dict, pm: dict) -> list[Opportunity]:
    opps = []
    poly_prices = parse_poly_prices(pm)
    if poly_prices is None:
        return opps
    poly_yes_cents, poly_no_cents = poly_prices

    k_yes_ask = km.get("yes_ask")
    k_no_ask = km.get("no_ask")
    if k_yes_ask is None or k_no_ask is None:
        return opps

    now = datetime.now(timezone.utc).isoformat()

    # Direction 1: Kalshi YES + Polymarket NO
    k_fee = kalshi_taker_fee_cents(k_yes_ask)
    combined = k_yes_ask + poly_no_cents
    total_cost = combined + k_fee + POLYMARKET_TAKER_FEE_CENTS
    net_edge = 100 - total_cost - SAFETY_MARGIN_CENTS
    if net_edge >= MIN_NET_EDGE_CENTS:
        opps.append(Opportunity(
            timestamp=now, match_score=round(score, 3),
            kalshi_ticker=km.get("ticker", ""), kalshi_title=km.get("title", ""),
            kalshi_close_time=km.get("close_time"),
            poly_question=pm.get("question", ""), poly_slug=pm.get("slug"),
            poly_end_date=pm.get("endDate"),
            direction="kalshi_YES + polymarket_NO",
            kalshi_leg_price_cents=k_yes_ask, poly_leg_price_cents=poly_no_cents,
            kalshi_fee_cents=round(k_fee, 2), poly_fee_cents=POLYMARKET_TAKER_FEE_CENTS,
            combined_cost_cents=round(combined, 2), net_edge_cents=round(net_edge, 2),
        ))

    # Direction 2: Kalshi NO + Polymarket YES
    k_fee2 = kalshi_taker_fee_cents(k_no_ask)
    combined2 = k_no_ask + poly_yes_cents
    total_cost2 = combined2 + k_fee2 + POLYMARKET_TAKER_FEE_CENTS
    net_edge2 = 100 - total_cost2 - SAFETY_MARGIN_CENTS
    if net_edge2 >= MIN_NET_EDGE_CENTS:
        opps.append(Opportunity(
            timestamp=now, match_score=round(score, 3),
            kalshi_ticker=km.get("ticker", ""), kalshi_title=km.get("title", ""),
            kalshi_close_time=km.get("close_time"),
            poly_question=pm.get("question", ""), poly_slug=pm.get("slug"),
            poly_end_date=pm.get("endDate"),
            direction="kalshi_NO + polymarket_YES",
            kalshi_leg_price_cents=k_no_ask, poly_leg_price_cents=poly_yes_cents,
            kalshi_fee_cents=round(k_fee2, 2), poly_fee_cents=POLYMARKET_TAKER_FEE_CENTS,
            combined_cost_cents=round(combined2, 2), net_edge_cents=round(net_edge2, 2),
        ))

    return opps


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, match_score REAL,
            kalshi_ticker TEXT, kalshi_title TEXT, kalshi_close_time TEXT,
            poly_question TEXT, poly_slug TEXT, poly_end_date TEXT,
            direction TEXT,
            kalshi_leg_price_cents REAL, poly_leg_price_cents REAL,
            kalshi_fee_cents REAL, poly_fee_cents REAL,
            combined_cost_cents REAL, net_edge_cents REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, kalshi_markets_fetched INTEGER,
            poly_markets_fetched INTEGER, candidates_considered INTEGER,
            opportunities_found INTEGER
        )
    """)
    conn.commit()


def save_opportunities(conn: sqlite3.Connection, opps: list[Opportunity]):
    for o in opps:
        conn.execute(
            """INSERT INTO opportunities
               (timestamp, match_score, kalshi_ticker, kalshi_title, kalshi_close_time,
                poly_question, poly_slug, poly_end_date, direction,
                kalshi_leg_price_cents, poly_leg_price_cents,
                kalshi_fee_cents, poly_fee_cents, combined_cost_cents, net_edge_cents)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (o.timestamp, o.match_score, o.kalshi_ticker, o.kalshi_title, o.kalshi_close_time,
             o.poly_question, o.poly_slug, o.poly_end_date, o.direction,
             o.kalshi_leg_price_cents, o.poly_leg_price_cents,
             o.kalshi_fee_cents, o.poly_fee_cents, o.combined_cost_cents, o.net_edge_cents),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    started = datetime.now(timezone.utc).isoformat()
    print(f"[{started}] fetching market data...")

    kalshi_markets = fetch_kalshi_markets()
    poly_markets = fetch_polymarket_markets()
    print(f"  kalshi: {len(kalshi_markets)} open markets")
    print(f"  polymarket: {len(poly_markets)} active markets")

    if not kalshi_markets or not poly_markets:
        print("One or both sources returned nothing this run — skipping matching.")
        candidates, all_opps = [], []
    else:
        candidates = find_candidate_matches(kalshi_markets, poly_markets)
        print(f"  candidate title matches above threshold: {len(candidates)}")

        all_opps: list[Opportunity] = []
        for score, km, pm in candidates:
            all_opps.extend(evaluate_pair(score, km, pm))

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    if all_opps:
        save_opportunities(conn, all_opps)
    conn.execute(
        "INSERT INTO run_log (timestamp, kalshi_markets_fetched, poly_markets_fetched, "
        "candidates_considered, opportunities_found) VALUES (?,?,?,?,?)",
        (started, len(kalshi_markets), len(poly_markets), len(candidates), len(all_opps)),
    )
    conn.commit()

    if all_opps:
        print(f"\n{len(all_opps)} hypothetical opportunity(ies) found this run:")
        for o in sorted(all_opps, key=lambda x: -x.net_edge_cents)[:10]:
            print(f"  net edge {o.net_edge_cents:+.2f}c | match {o.match_score:.2f} | "
                  f"{o.direction} | Kalshi: {o.kalshi_title!r} <-> Poly: {o.poly_question!r}")
    else:
        print("\nNo opportunities cleared the fee+margin threshold this run. That is normal —")
        print("that's exactly the signal this scanner exists to log honestly over time.")

    conn.close()


if __name__ == "__main__":
    main()
