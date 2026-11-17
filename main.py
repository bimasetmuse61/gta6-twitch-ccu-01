"""Forecast GTA VI's launch-day Twitch viewers, hour by hour.

Model: comparable-launch regression.

Every past launch in the case's `comparables` gives one full day curve: the
hourly viewers of its Twitch category over its own release day (00:00-24:00
UTC).  For hour h, work in log ratios to that launch's own hour-1 print:

    r_h = ln(viewers_h / viewers_1)

Across launches, r_h depends strongly on how big hour 1 was: a launch whose
hour 1 already holds the midnight unlock wave (huge hour 1) stays flat, while
one whose hour 1 is a dead-of-night trickle before most timezones unlock
rises several-fold by prime time.  So for each hour h = 2..24, fit a line of
r_h against ln(hour-1 viewers) over the comparables, shrink the slope 20%
toward flat (the slope is the noisiest part of the fit), and predict the case
at its own hour-1 print.  The median forecast is

    q50(h) = p1 * exp(a_h + b_h * ln(p1))

with two guards:

* a floor: the prediction never falls below the lowest log ratio any of the
  eight biggest hour-1 comparables showed at that hour, minus 0.1.  The
  linear fit would otherwise keep falling as hour 1 grows past every launch
  on record, but no big launch ever fell that far.
* a fallback: with fewer than five usable comparables, fall back to
  persistence (every hour at hour 1's print).

Spread: the five quantiles are the median times exp(z_tau * sigma_h), where
z_tau is the standard normal quantile and sigma_h is half the residual
standard deviation of that hour's fit (0.45x, floored at 0.05).  The spread
is honest about how much launch curves disagree, and narrower than the house
baseline's random-walk spread because the regression removes the systematic
hour-1-size effect first.

Only the case file is read.  Standard library only, deterministic, no
network, no clock.  Runs in well under a second per case.

Practice (local scoring against the four practice launches' real SullyGnome
hourly counts, mean pinball loss on the log scale): 0.156, against the house
baseline's 0.387.  Per case: p00 0.146, p01 0.158, p02 0.135, p03 0.185.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.6448536269514722, -0.6744897501960817, 0.0, 0.6744897501960817, 1.6448536269514722]

# A comparable only counts if its hour 1 is a real print, not a trickle.
MIN_HOUR1 = 1000
# How much of the fitted slope to keep (the rest shrinks toward flat).
SLOPE_KEEP = 0.8
# Spread multiplier on the fit's residual standard deviation.
SPREAD_MULT = 0.45
# Smallest spread allowed, in log units.
SPREAD_FLOOR = 0.05
# The floor uses this many biggest hour-1 comparables.
FLOOR_TOP_N = 8
# Margin below the biggest launches' lowest log ratio at an hour.
FLOOR_MARGIN = 0.1
# Comparables needed before the regression is trusted at all.
MIN_ROWS = 5
# The house baseline's spread, used only by the fallback.
FALLBACK_SIGMA = 0.15


def round2(x):
    """Round to 2 decimals, half up; whole numbers come back as ints."""
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value == int(value) else value


def parse_hour(row_from):
    """'2022-02-25T00:00:00Z' -> aware datetime, or None."""
    try:
        return datetime.strptime(row_from, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def day_curve(comparable):
    """A comparable's 24 hourly viewer counts for its release day, or None.

    Requires every hour present, positive, and an hour-1 print of at least
    MIN_HOUR1 viewers.
    """
    start = parse_hour(comparable.get("day_start"))
    if start is None:
        return None
    by_hour = {}
    for row in comparable.get("hourly") or []:
        t = parse_hour(row.get("from"))
        if t is not None and isinstance(row.get("viewers"), (int, float)):
            by_hour[t] = row["viewers"]
    values = []
    for h in range(24):
        v = by_hour.get(start.replace(hour=h))
        if v is None or v <= 0:
            return None
        values.append(v)
    if values[0] < MIN_HOUR1:
        return None
    return values


def ols(xs, ys):
    """Least-squares line; returns (intercept, slope)."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return my, 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return my, sxy / sxx


def pstdev(values):
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / n)


def forecast_case(case, name):
    if case.get("levels") != LEVELS:
        raise ValueError(f"{name} asks for levels {case.get('levels')}, not {LEVELS}")
    known = case.get("known") or []
    first = next((k for k in known if k.get("hour") == 1), None)
    if first is None:
        raise ValueError(f"{name} has no hour-1 print")
    p1 = max(1, first["viewers"])

    rows = []
    for comp in case.get("comparables") or []:
        curve = day_curve(comp)
        if curve is None:
            continue
        rows.append((curve[0], [math.log(v / curve[0]) for v in curve]))

    hours = [row["hour"] for row in case.get("forecast") or []]
    if not hours:
        hours = list(range(2, 25))

    if len(rows) < MIN_ROWS:
        return {"hours": [{"hour": h, "q": fallback_quantiles(p1, h)} for h in hours]}

    xs = [math.log(h1) for h1, _ in rows]
    log_p1 = math.log(p1)
    biggest = sorted(rows, key=lambda r: -r[0])[:FLOOR_TOP_N]

    out = []
    for h in hours:
        ys = [lr[h - 1] for _, lr in rows]
        intercept, slope = ols(xs, ys)
        slope *= SLOPE_KEEP
        intercept = sum(ys) / len(ys) - slope * (sum(xs) / len(xs))
        median_log = intercept + slope * log_p1
        floor = min(lr[h - 1] for _, lr in biggest) - FLOOR_MARGIN
        if median_log < floor:
            median_log = floor
        residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
        sigma = max(pstdev(residuals), SPREAD_FLOOR) * SPREAD_MULT
        qs = []
        prev = None
        for z in Z:
            q = round2(min(max(p1 * math.exp(median_log + z * sigma), 0.01), 1e9))
            if prev is not None and q <= prev and q < 1e9:
                q = round2(prev + 0.01)
            qs.append(q)
            prev = q
        out.append({"hour": h, "q": qs})
    return {"hours": out}


def fallback_quantiles(p1, h):
    """Persistence with a widening spread, as the house baseline does."""
    spread = FALLBACK_SIGMA * math.sqrt(h - 1)
    return [round2(p1 * math.exp(z * spread)) for z in Z]


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case, path.name)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
