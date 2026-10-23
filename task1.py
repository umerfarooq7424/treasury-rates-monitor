# TASK 1: US TREASURY RATES MONITOR
#
# Install dependencies in the VS Code terminal if needed:
# python -m pip install pandas matplotlib requests
#
# Run:
# python task1.py

from pathlib import Path
from datetime import date, datetime, timezone
from io import BytesIO
import json

import pandas as pd
from urllib.request import urlopen
import argparse
import hashlib
import matplotlib

# Save charts without requiring an interactive plot window.
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.dates as mdates


# ============================================================
# 1. SETTINGS AND FOLDERS
# ============================================================

parser = argparse.ArgumentParser(description="Treasury rates monitor")
parser.add_argument("--csv", type=Path, help="Use a saved CSV without downloading")
parser.add_argument("--end", default=date.today().isoformat(), help="Requested endpoint YYYY-MM-DD")
args = parser.parse_args()
START = "2020-01-01"
END = pd.Timestamp(args.end).date().isoformat()

PROJECT = Path(__file__).resolve().parent / "rates_monitor"
RAW = PROJECT / "data" / "raw"
OUTPUT = PROJECT / "outputs"

RAW.mkdir(parents=True, exist_ok=True)
OUTPUT.mkdir(parents=True, exist_ok=True)

RAW_PATH = args.csv.resolve() if args.csv else RAW / f"fred_treasury_{START}_to_{END}.csv"
if args.csv and not RAW_PATH.is_file():
    raise FileNotFoundError(RAW_PATH)

URL = (
    "https://fred.stlouisfed.org/graph/fredgraph.csv"
    f"?id=DGS2,DGS10&cosd={START}&coed={END}"
)

HORIZONS = {
    1: "1 observation",
    5: "5 observations (~1 week)",
    21: "21 observations (~1 month)",
}

plt.style.use("seaborn-v0_8-whitegrid")


# ============================================================
# 2. DOWNLOAD SOURCE DATA
# ============================================================

if RAW_PATH.exists():
    print("Using saved source snapshot.")
else:
    print("Downloading source data...")

    with urlopen(URL, timeout=60) as response:
        payload = response.read()

    preview = pd.read_csv(BytesIO(payload))

    if not {"DGS2", "DGS10"}.issubset(preview.columns):
        raise ValueError("FRED response does not contain both yield series.")

    RAW_PATH.write_bytes(payload)

    metadata = {
        "source_url": URL,
        "retrieved_utc": datetime.now(timezone.utc).isoformat(),
        "requested_start": START,
        "requested_end": END,
        "yield_units": "Percent",
        "series": ["DGS2", "DGS10"],
    }

    RAW_PATH.with_suffix(".json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

raw = pd.read_csv(RAW_PATH, na_values=["."])
(OUTPUT / "run_provenance.json").write_text(json.dumps({
    "run_utc": datetime.now(timezone.utc).isoformat(),
    "source_filename": RAW_PATH.name,
    "source_sha256": hashlib.sha256(RAW_PATH.read_bytes()).hexdigest(),
    "requested_start": START, "requested_end": END,
    "mode": "uploaded snapshot" if args.csv else "FRED download/cache",
    "note": "Snapshot reproduction is not independent source verification."
}, indent=2), encoding="utf-8")

date_column = next(
    (
        column
        for column in ["observation_date", "DATE"]
        if column in raw.columns
    ),
    None,
)

if date_column is None:
    raise ValueError("Date column not found in the downloaded file.")

if not {"DGS2", "DGS10"}.issubset(raw.columns):
    raise ValueError("Yield columns missing from the saved file.")


# ============================================================
# 3. CLEAN DATA AND FILTER DATES
# ============================================================

df = raw.rename(columns={date_column: "date"}).copy()
df["date"] = pd.to_datetime(df["date"], errors="raise")

if df["date"].isna().any():
    raise ValueError("Missing dates found.")

for column in ["DGS2", "DGS10"]:
    df[column] = pd.to_numeric(df[column], errors="raise")

df = (
    df.loc[df["date"].between(START, END), ["date", "DGS2", "DGS10"]]
    .sort_values("date")
    .reset_index(drop=True)
)

if df.empty:
    raise ValueError("No observations within the requested period.")

if df["date"].duplicated().any():
    raise ValueError("Duplicate dates found.")

if df[["DGS2", "DGS10"]].isin(
    [float("inf"), float("-inf")]
).any().any():
    raise ValueError("Infinite yield values found.")

missing = df[["DGS2", "DGS10"]].isna().any(axis=1)

# Keep FULL HISTORY in 'clean'.
# The recent chart will use a separate dataframe called 'recent'.
clean = df.loc[~missing].copy().reset_index(drop=True)

if len(clean) < 22:
    raise ValueError("At least 22 complete observations are required.")

AS_OF = clean["date"].max().strftime("%Y-%m-%d")

qc = pd.DataFrame({
    "Check": [
        "Requested start",
        "Requested end",
        "Source rows within period",
        "Duplicate dates",
        "Missing 2-year yields",
        "Missing 10-year yields",
        "Rows excluded",
        "Complete observations",
        "First complete date",
        "Last complete date",
    ],
    "Result": [
        START,
        END,
        len(df),
        int(df["date"].duplicated().sum()),
        int(df["DGS2"].isna().sum()),
        int(df["DGS10"].isna().sum()),
        int(missing.sum()),
        len(clean),
        clean["date"].min().strftime("%Y-%m-%d"),
        AS_OF,
    ],
})

print("\n--- DATA QUALITY CHECKS ---")
print(qc.to_string(index=False))

qc.to_csv(OUTPUT / f"quality_checks_{AS_OF}.csv", index=False)


# ============================================================
# 4. INVESTIGATE MISSING OBSERVATIONS
# ============================================================

audit = df.copy()
audit["missing_2y"] = audit["DGS2"].isna()
audit["missing_10y"] = audit["DGS10"].isna()

audit["both_missing"] = audit["missing_2y"] & audit["missing_10y"]
audit["only_one_missing"] = audit["missing_2y"] ^ audit["missing_10y"]
audit["year"] = audit["date"].dt.year

annual_qc = audit.groupby("year").agg(
    source_rows=("date", "size"),
    both_missing=("both_missing", "sum"),
    only_one_missing=("only_one_missing", "sum"),
)

annual_qc["complete_rows"] = (
    annual_qc["source_rows"]
    - annual_qc["both_missing"]
    - annual_qc["only_one_missing"]
)

annual_qc["excluded_pct"] = (
    100
    * (annual_qc["both_missing"] + annual_qc["only_one_missing"])
    / annual_qc["source_rows"]
).round(2)

missing_detail = audit.loc[
    audit["missing_2y"] | audit["missing_10y"],
    ["date", "DGS2", "DGS10", "both_missing", "only_one_missing"],
].copy()

missing_detail["weekday"] = missing_detail["date"].dt.day_name()

# Screening check through the last returned source date.
# This is not a validated market holiday calendar.
expected_weekdays = pd.bdate_range(START, df["date"].max())
absent_dates = expected_weekdays.difference(
    pd.DatetimeIndex(df["date"])
)
absent_detail = pd.DataFrame({"date": absent_dates})

gap_review = clean[["date"]].copy()
gap_review["previous_date"] = gap_review["date"].shift(1)
gap_review["calendar_days"] = (
    gap_review["date"] - gap_review["previous_date"]
).dt.days

long_gaps = gap_review.loc[gap_review["calendar_days"] > 3].copy()

print("\n--- MISSING DATA ALIGNMENT ---")
print(f"Both yields missing: {audit['both_missing'].sum()}")
print(f"Only one yield missing: {audit['only_one_missing'].sum()}")
print(f"Weekdays absent within source coverage: {len(absent_detail)}")

print("\n--- MISSING DATA BY YEAR ---")
print(annual_qc.to_string())

annual_qc.to_csv(OUTPUT / f"annual_quality_review_{AS_OF}.csv")
missing_detail.to_csv(
    OUTPUT / f"missing_dates_review_{AS_OF}.csv", index=False
)
absent_detail.to_csv(
    OUTPUT / f"absent_weekdays_review_{AS_OF}.csv", index=False
)
long_gaps.to_csv(
    OUTPUT / f"long_gaps_review_{AS_OF}.csv", index=False
)


# ============================================================
# 5. SPREAD AND MULTI-HORIZON CHANGES
# ============================================================

clean["spread_bp"] = (clean["DGS10"] - clean["DGS2"]) * 100

for periods in HORIZONS:
    clean[f"comparison_date_{periods}"] = clean["date"].shift(periods)
    clean[f"change_2y_{periods}_bp"] = clean["DGS2"].diff(periods) * 100
    clean[f"change_10y_{periods}_bp"] = clean["DGS10"].diff(periods) * 100
    clean[f"spread_change_{periods}_bp"] = clean["spread_bp"].diff(periods)

    error = (
        clean[f"spread_change_{periods}_bp"]
        - (
            clean[f"change_10y_{periods}_bp"]
            - clean[f"change_2y_{periods}_bp"]
        )
    ).dropna()

    if not error.abs().lt(1e-8).all():
        raise ValueError(f"Reconciliation failed for horizon {periods}.")


def classify_move(change_2y, change_10y):
    tolerance = 1e-8
    spread_change = change_10y - change_2y

    if abs(spread_change) < tolerance:
        if change_2y > tolerance:
            return "Parallel rise in yields"
        if change_2y < -tolerance:
            return "Parallel fall in yields"
        return "No change"

    shape = "steepening" if spread_change > 0 else "flattening"

    if change_2y > tolerance and change_10y > tolerance:
        return f"Bear {shape}"
    if change_2y < -tolerance and change_10y < -tolerance:
        return f"Bull {shape}"

    return f"Mixed/one-leg move: {shape}"


# ============================================================
# 6. CLIENT MONITOR TABLE
# ============================================================

latest = clean.iloc[-1]
last_date = latest["date"]
lookback_start = last_date - pd.DateOffset(years=2)

rows = []

for periods, label in HORIZONS.items():
    previous = clean.iloc[-1 - periods]

    change_2y = latest[f"change_2y_{periods}_bp"]
    change_10y = latest[f"change_10y_{periods}_bp"]
    spread_change = latest[f"spread_change_{periods}_bp"]

    history = clean.loc[
        (clean["date"] >= lookback_start)
        & (clean["date"] <= previous["date"]),
        f"spread_change_{periods}_bp",
    ].dropna()

    magnitudes = history.abs().round(6)
    current_magnitude = round(abs(spread_change), 6)

    percentile = float("nan")

    if len(history) >= 100:
        percentile = 100 * (
            (magnitudes < current_magnitude).sum()
            + 0.5 * (magnitudes == current_magnitude).sum()
        ) / len(history)

    rows.append({
        "Horizon": label,
        "From": previous["date"].strftime("%Y-%m-%d"),
        "To": AS_OF,
        "Calendar days": (last_date - previous["date"]).days,
        "2Y change (bp)": change_2y,
        "10Y change (bp)": change_10y,
        "Spread change (bp)": spread_change,
        "Curve move": classify_move(change_2y, change_10y),
        "Absolute move percentile": percentile,
        "Historical comparisons": len(history),
    })

summary = pd.DataFrame(rows)

print("\n--- LATEST LEVELS ---")
print(f"As of: {AS_OF}")
print(f"2-year yield: {latest['DGS2']:.2f}%")
print(f"10-year yield: {latest['DGS10']:.2f}%")
print(f"2s10s spread: {latest['spread_bp']:+.1f} bp")

print("\n--- MULTI-HORIZON CLIENT MONITOR ---")
print(summary.round(1).to_string(index=False))

clean.to_csv(
    OUTPUT / f"treasury_rates_analysis_{AS_OF}.csv", index=False
)
summary.round(2).to_csv(
    OUTPUT / f"client_monitor_{AS_OF}.csv", index=False
)


# ============================================================
# 7. REUSABLE CHART FUNCTION
# ============================================================

def draw_panels(yield_ax, spread_ax, data, title, show_zero=True):
    yield_ax.plot(
        data["date"], data["DGS2"],
        label="2-year", color="#2563EB", linewidth=1.7,
    )
    yield_ax.plot(
        data["date"], data["DGS10"],
        label="10-year", color="#0F766E", linewidth=1.7,
    )
    yield_ax.set_title(title, loc="left", fontweight="bold")
    yield_ax.set_ylabel("Yield (%)")
    yield_ax.legend(frameon=False, ncol=2)

    spread_ax.plot(
        data["date"], data["spread_bp"],
        color="#334155", linewidth=1.7,
    )

    if show_zero or data["spread_bp"].min() <= 0 <= data["spread_bp"].max():
        spread_ax.axhline(
            0, color="#64748B", linestyle="--", linewidth=1,
        )

    if (data["spread_bp"] < 0).any():
        spread_ax.fill_between(
            data["date"], data["spread_bp"], 0,
            where=data["spread_bp"] < 0,
            interpolate=True, color="#DC2626", alpha=0.12,
        )

    spread_ax.set_ylabel("10Y minus 2Y (bp)")
    spread_ax.set_xlabel("Observation date")

    for ax in [yield_ax, spread_ax]:
        ax.grid(alpha=0.25)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.margins(y=0.12)


# ============================================================
# 8. SAVE THE FULL HISTORICAL CHART
# ============================================================

fig, axes = plt.subplots(
    2, 1,
    figsize=(12, 8),
    sharex=True,
    gridspec_kw={"height_ratios": [1.2, 1]},
)

draw_panels(
    axes[0], axes[1], clean,
    f"Full history: {clean['date'].min():%d %b %Y} to {last_date:%d %b %Y}",
)

axes[1].set_xlim(pd.Timestamp(START), last_date + pd.Timedelta(days=10))
axes[1].xaxis.set_major_locator(mdates.YearLocator())
axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

fig.suptitle(
    "US Treasury yields and the 2s10s spread",
    fontsize=17, fontweight="bold",
)
fig.text(
    0.08, 0.025,
    "Source: Federal Reserve H.15 via FRED (DGS2, DGS10).\n"
    "Complete paired observations; no forward-fill. Lines connect valid observations.",
    fontsize=9, color="#475569",
)
fig.tight_layout(rect=[0, 0.08, 1, 0.94])

history_png = OUTPUT / f"01_full_history_{AS_OF}.png"
history_pdf = OUTPUT / f"01_full_history_{AS_OF}.pdf"

fig.savefig(history_png, dpi=300, bbox_inches="tight")
fig.savefig(history_pdf, bbox_inches="tight")
plt.close(fig)


# ============================================================
# 9. SAVE HISTORY AND THREE-MONTH VIEW SIDE BY SIDE
# ============================================================

recent_start = last_date - pd.DateOffset(months=3)

# IMPORTANT: Do not overwrite 'clean' with this filtered data.
recent = clean.loc[clean["date"] >= recent_start].copy()

fig, axes = plt.subplots(
    2, 2,
    figsize=(16, 9),
    sharex="col",
    gridspec_kw={"height_ratios": [1.2, 1]},
)

# Left column: ALL observations from 2020.
draw_panels(
    axes[0, 0], axes[1, 0], clean,
    "Full history since 2020",
)

# Right column: ONLY the recent three months.
draw_panels(
    axes[0, 1], axes[1, 1], recent,
    "Latest three months",
    show_zero=False,
)

# Set independent horizontal ranges explicitly.
axes[1, 0].set_xlim(
    pd.Timestamp(START), last_date + pd.Timedelta(days=10)
)
axes[1, 1].set_xlim(
    recent_start, last_date + pd.Timedelta(days=7)
)

axes[1, 0].xaxis.set_major_locator(mdates.YearLocator())
axes[1, 0].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

axes[1, 1].xaxis.set_major_locator(mdates.MonthLocator())
axes[1, 1].xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))

# Highlight the latest five-observation comparison window.
week_start = clean.iloc[-6]["date"]

for ax in axes[:, 1]:
    ax.axvspan(
        week_start, last_date,
        color="#F59E0B", alpha=0.14,
    )

# Label the latest yield levels.
for series, color, offset in [
    ("DGS2", "#2563EB", -16),
    ("DGS10", "#0F766E", 10),
]:
    axes[0, 1].scatter(
        last_date, latest[series], color=color, s=28, zorder=5,
    )
    axes[0, 1].annotate(
        f"{latest[series]:.2f}%",
        xy=(last_date, latest[series]),
        xytext=(-8, offset),
        textcoords="offset points",
        ha="right", color=color, fontweight="bold",
    )

axes[1, 1].scatter(
    last_date, latest["spread_bp"],
    color="#334155", s=28, zorder=5,
)
axes[1, 1].annotate(
    f"{latest['spread_bp']:+.1f} bp",
    xy=(last_date, latest["spread_bp"]),
    xytext=(-8, 10),
    textcoords="offset points",
    ha="right", fontweight="bold",
)

fig.suptitle(
    f"US Treasury monitor | As of {AS_OF}",
    fontsize=18, fontweight="bold", y=0.98,
)
fig.text(
    0.5, 0.93,
    "Latest five-observation changes: "
    f"2Y {latest['change_2y_5_bp']:+.1f} bp  |  "
    f"10Y {latest['change_10y_5_bp']:+.1f} bp  |  "
    f"Spread {latest['spread_change_5_bp']:+.1f} bp",
    ha="center", fontsize=12, color="#334155",
)
fig.text(
    0.07, 0.025,
    "Source: Federal Reserve H.15 via FRED (DGS2, DGS10). "
    "Complete paired observations; no forward-fill.\n"
    "Amber shading: latest five-observation window. "
    "Panels use independent vertical scales. Lines connect valid observations.",
    fontsize=9, color="#475569",
)
fig.tight_layout(rect=[0.02, 0.09, 0.98, 0.90])

comparison_png = OUTPUT / f"02_history_and_recent_{AS_OF}.png"
comparison_pdf = OUTPUT / f"02_history_and_recent_{AS_OF}.pdf"

fig.savefig(comparison_png, dpi=300, bbox_inches="tight")
fig.savefig(comparison_pdf, bbox_inches="tight")
plt.close(fig)


# ============================================================
# 10. SAVE THE CLIENT BRIEFING AND METHODOLOGY
# ============================================================

weekly = summary.loc[
    summary["Horizon"] == HORIZONS[5]
].iloc[0]

briefing = (
    f"TREASURY CURVE CLIENT BRIEFING\nAs of {AS_OF}\n\n"
    f"Two-year yields stood at {latest['DGS2']:.2f}% and ten-year yields "
    f"at {latest['DGS10']:.2f}%, with a spread of "
    f"{latest['spread_bp']:+.1f} bp.\n\n"
    f"From {weekly['From']} to {weekly['To']}, two-year yields moved "
    f"{weekly['2Y change (bp)']:+.1f} bp and ten-year yields moved "
    f"{weekly['10Y change (bp)']:+.1f} bp. The spread changed by "
    f"{weekly['Spread change (bp)']:+.1f} bp "
    f"({weekly['Curve move'].lower()}).\n\n"
    "Client implication: Review exposure by maturity. Both yield levels "
    "and curve shape matter; assess holdings, key-rate DV01, and hedges "
    "before estimating portfolio impact. These observations do not "
    "establish the cause of market moves or predict future returns."
)

methodology = (
    f"Requested period: {START} through {END}.\n"
    f"Actual complete coverage: {clean['date'].min():%Y-%m-%d} "
    f"through {AS_OF}.\n\n"
    "Source: Federal Reserve H.15 via FRED, DGS2 and DGS10. "
    "Constant-maturity yields are not individual bond returns.\n\n"
    "Dates are filtered locally. Missing pairs are preserved for audit "
    "and excluded without forward-filling. Missing dates have not been "
    "fully reconciled against the source publication calendar. "
    "Structural checks do not verify every numeric observation.\n\n"
    "Changes use 1, 5, and 21 complete observations. These approximate "
    "daily, weekly, and monthly periods; actual dates are displayed.\n\n"
    "Absolute spread changes are ranked against same-horizon changes "
    "ending within the preceding two calendar years. Windows overlapping "
    "the current move are excluded. Ties use midranks. Historical windows "
    "overlap one another, so percentiles are descriptive, not independent "
    "statistical tests, forecasts, or trading signals.\n\n"
    "The downloaded snapshot is not a point-in-time vintage dataset."
)

(OUTPUT / f"weekly_briefing_{AS_OF}.txt").write_text(
    briefing, encoding="utf-8",
)
(OUTPUT / f"methodology_{AS_OF}.txt").write_text(
    methodology, encoding="utf-8",
)


# ============================================================
# 11. CONFIRM SAVED CHARTS
# ============================================================

for chart in [history_png, history_pdf, comparison_png, comparison_pdf]:
    if not chart.exists() or chart.stat().st_size == 0:
        raise RuntimeError(f"Chart was not saved correctly: {chart}")

print("\n--- FINISHED: OPEN THESE IMAGE FILES ---")
print(f"\nFULL HISTORICAL CHART:\n{history_png}")
print(f"\nHISTORY BESIDE THREE-MONTH VIEW:\n{comparison_png}")
print(f"\nAll deliverables are saved in:\n{OUTPUT}")
print("\nCharts are saved as files; no plot window will open automatically.")
# Within-week path: six observations define five changes.
week = clean.tail(6).copy()
path_stats = pd.DataFrame([{
    "from": week.iloc[0]["date"], "to": week.iloc[-1]["date"],
    "net_spread_change_bp": week.iloc[-1]["spread_bp"]-week.iloc[0]["spread_bp"],
    "min_spread_bp": week["spread_bp"].min(),
    "max_spread_bp": week["spread_bp"].max(),
    "range_bp": week["spread_bp"].max()-week["spread_bp"].min(),
    "sum_absolute_changes_bp": week["spread_bp"].diff().abs().sum(),
}])
week[["date","DGS2","DGS10","spread_bp"]].to_csv(OUTPUT / "weekly_path.csv", index=False)
path_stats.round(2).to_csv(OUTPUT / "weekly_path_summary.csv", index=False)
print("\n--- WITHIN-WEEK SPREAD PATH ---")
print(path_stats.round(2).to_string(index=False))
