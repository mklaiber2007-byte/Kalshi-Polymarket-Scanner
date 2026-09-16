#!/usr/bin/env python3
"""
Summarize scanner_log.db after the GitHub Actions cron job has accumulated
some history. Run this locally after pulling the latest commits:

    git pull
    python3 report.py

This answers the question the whole paper-trading phase exists to answer:
does a real, repeatable, fee-adjusted edge show up over time, or is it
mostly noise, stale quotes, and false-positive title matches?
"""

import sqlite3
import sys

DB_PATH = "scanner_log.db"


def main():
    try:
        conn = sqlite3.connect(DB_PATH)
    except sqlite3.Error as e:
        print(f"Could not open {DB_PATH}: {e}", file=sys.stderr)
        sys.exit(1)

    runs = conn.execute("SELECT COUNT(*) FROM run_log").fetchone()[0]
    if runs == 0:
        print("No runs logged yet. Let the GitHub Actions cron job run a few times, "
              "then `git pull` and re-run this.")
        return

    total_opps = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    avg_edge = conn.execute("SELECT AVG(net_edge_cents) FROM opportunities").fetchone()[0]
    max_edge = conn.execute("SELECT MAX(net_edge_cents) FROM opportunities").fetchone()[0]
    first_run = conn.execute("SELECT MIN(timestamp) FROM run_log").fetchone()[0]
    last_run = conn.execute("SELECT MAX(timestamp) FROM run_log").fetchone()[0]

    print(f"Runs logged:           {runs}")
    print(f"Window:                {first_run}  ->  {last_run}")
    print(f"Opportunities flagged: {total_opps}")
    if total_opps:
        print(f"Average net edge:      {avg_edge:.2f} cents/contract")
        print(f"Best net edge seen:    {max_edge:.2f} cents/contract")

    print("\nMost frequently repeating matched pairs (possible persistent mispricing,")
    print("OR a recurring false-positive title match worth tightening the threshold on):")
    rows = conn.execute("""
        SELECT kalshi_title, poly_question, COUNT(*) as n, AVG(net_edge_cents) as avg_edge
        FROM opportunities
        GROUP BY kalshi_title, poly_question
        ORDER BY n DESC
        LIMIT 10
    """).fetchall()
    for kalshi_title, poly_question, n, avg_edge in rows:
        print(f"  x{n:<3} avg {avg_edge:+.2f}c | {kalshi_title!r} <-> {poly_question!r}")

    conn.close()


if __name__ == "__main__":
    main()
