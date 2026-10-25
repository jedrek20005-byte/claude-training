"""Turns insider purchases into ranked buy signals.

A stock qualifies when either
  * several different insiders (officers/directors) bought within a short window, or
  * the CEO or CFO alone bought a large amount.
Research on insider trading finds these "cluster" and top-executive purchases are the
most informative; routine buys by large funds (10% owners) are ignored.
"""
import re
from datetime import timedelta

import numpy as np
import pandas as pd

CEO_CFO = re.compile(r"\b(?:ceo|cfo|chief executive|chief financial)\b", re.I)
SIGNAL_COLUMNS = ["ticker", "n_insiders", "total_value", "ceo_cfo", "last_filing", "insiders", "score"]


def find_signals(tx: pd.DataFrame, as_of, cfg: dict) -> pd.DataFrame:
    """Signals known on `as_of` (only filings up to that date are used), best first."""
    s = cfg["signals"]
    as_of = pd.Timestamp(as_of).normalize()
    if tx.empty:
        return pd.DataFrame(columns=SIGNAL_COLUMNS)

    df = tx[
        (tx["code"] == "P")
        & (tx["acq_disp"] != "D")
        & (tx["price"] >= s["min_price"])
        & (tx["filing_date"] <= as_of)
        & (tx["trans_date"] >= as_of - timedelta(days=s["lookback_days"]))
        & (tx["is_officer"] | tx["is_director"])
        & tx["ticker"].str.fullmatch(r"[A-Z]{1,5}", na=False)
    ]
    if df.empty:
        return pd.DataFrame(columns=SIGNAL_COLUMNS)
    df = df.assign(ceo_cfo=df["officer_title"].fillna("").str.contains(CEO_CFO))

    per_owner = df.groupby(["ticker", "owner_cik"]).agg(
        value=("value", "sum"),
        ceo_cfo=("ceo_cfo", "max"),
        last_filing=("filing_date", "max"),
        name=("owner_name", "first"),
    ).reset_index()
    per_owner = per_owner[per_owner["value"] >= s["min_value_per_insider"]]
    if per_owner.empty:
        return pd.DataFrame(columns=SIGNAL_COLUMNS)
    per_owner["ceo_cfo_value"] = per_owner["value"].where(per_owner["ceo_cfo"], 0.0)

    g = per_owner.groupby("ticker").agg(
        n_insiders=("owner_cik", "nunique"),
        total_value=("value", "sum"),
        ceo_cfo=("ceo_cfo", "max"),
        ceo_cfo_value=("ceo_cfo_value", "max"),
        last_filing=("last_filing", "max"),
        insiders=("name", lambda names: ", ".join(sorted(set(names)))),
    ).reset_index()

    cluster = (g["n_insiders"] >= s["min_insiders"]) & (g["total_value"] >= s["min_cluster_value"])
    solo = g["ceo_cfo_value"] >= s["solo_ceo_cfo_min_value"]
    fresh = g["last_filing"] >= as_of - timedelta(days=s["max_signal_age_days"])
    g = g[(cluster | solo) & fresh].copy()

    g["score"] = (np.log10(g["total_value"])
                  + 0.5 * (g["n_insiders"].clip(upper=5) - 1)
                  + g["ceo_cfo"].astype(float))
    return g.sort_values("score", ascending=False)[SIGNAL_COLUMNS].reset_index(drop=True)
