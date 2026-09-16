# Kalshi ↔ Polymarket Paper-Trading Scanner

A free, $0-capital tool that watches for cross-platform pricing gaps between
Kalshi and Polymarket and **logs them as hypothetical trades** — it never
places a real order or touches an API key. This is Phase 5–7 of a proper
EV-testing process: prove an edge exists in a running log before risking
real money on it.

## What it actually checks

For markets that appear (by fuzzy title match) to describe the same
real-world event, it checks whether buying the opposite side on each
platform costs less than $1.00 combined, after an approximate fee estimate
and a safety margin for slippage/stale quotes. If so, it logs the
hypothetical trade with full math to `scanner_log.db`.

## Honest limitations, read before trusting any output

- **Title matching is approximate.** Two markets can read as nearly
  identical while settling on different rules, sources, or dates. Every
  flagged pair is a *lead to manually check*, not a confirmed match.
- **Fee constants are approximations** of publicly documented formulas at
  the time this was written. Both platforms have changed fees during 2026.
  Verify current rates before trusting a number.
- **This polls every 15 minutes.** Public research on this strategy in 2026
  describes competitive windows closing in single-digit seconds on liquid
  markets — a 15-minute cron job on free infrastructure will mostly miss
  those and will disproportionately catch stale-quote false positives.
  That's a feature for this phase, not a bug: it tells you honestly what a
  $0-infrastructure setup can and can't see, before you spend anything.
- **No execution.** Turning any of this into real trades means: funding
  real accounts on both platforms (realistic minimums discussed in most
  current write-ups run $500–$5,000+ split across both), writing an
  execution layer with your own API keys, and accepting that you are then
  competing against faster, better-capitalized bots — not printing free
  money.

## Setup (all free)

1. Create a new **public** GitHub repo (public repos get unlimited free
   Actions minutes; private repos have a monthly limit that's still plenty
   for a 15-minute cron on a script this light, but public is simplest).
2. Push these files to it: `scanner.py`, `report.py`, `requirements.txt`,
   `.github/workflows/scan.yml`.
3. That's it — no secrets, no API keys, nothing to configure. The workflow
   runs itself every 15 minutes and commits `scanner_log.db` back to the
   repo each time it finds something.
4. To check results:
   ```bash
   git pull
   python3 report.py
   ```
5. To run it once by hand instead of waiting for the cron: open the repo's
   **Actions** tab → this workflow → **Run workflow**.

## Deciding what to do with the results

After a couple of weeks of logged runs, look at `report.py`'s output:

- **Few or no opportunities, or edge never repeats on the same pair** →
  matches the more skeptical research on this strategy: retail-accessible
  edge is thin and fast-competed-away. Don't fund real accounts on the
  strength of this alone.
- **A specific pair repeats with consistent positive edge across many
  runs, on a market you've manually verified settles identically on both
  platforms** → that's the signal worth taking to a human decision about
  funding real accounts, sized to what you can afford to have locked up
  until the market resolves (which can take weeks to months).

This tool's job ends at "here's the evidence." Whether to put real money
behind it is a financial decision only you should make, sized to money you
can afford to lose.
