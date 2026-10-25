"""Downloads and parses SEC Form 4 insider filings (free, official source).

Live mode reads the EDGAR daily index and each Form 4 filed that day.
Only open-market purchases (transaction code "P") are kept.
"""
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

SEC = "https://www.sec.gov"
COLUMNS = ["ticker", "issuer_cik", "owner_cik", "owner_name", "is_director", "is_officer",
           "is_ten_pct", "officer_title", "trans_date", "filing_date", "code", "acq_disp",
           "shares", "price", "value", "accession"]


class SecClient:
    """HTTP client that respects the SEC fair-access rules (contact UA, <10 requests/s)."""

    def __init__(self, user_agent: str, min_interval: float = 0.12):
        if "@" not in user_agent:
            raise ValueError("SEC_USER_AGENT must contain a contact email, e.g. 'my-bot me@example.com'")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.min_interval = min_interval
        self._last = 0.0

    def get(self, url: str, binary: bool = False):
        """Returns text (or bytes), or None if the document does not exist."""
        for attempt in range(5):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(url, timeout=60)
            except requests.RequestException as e:
                log.warning("SEC request error (%s), retrying: %s", e, url)
                time.sleep(2 ** attempt * 2)
                continue
            if r.status_code == 404:
                return None
            # The SEC answers 403 when it throttles a client, so back off on it too.
            if r.status_code in (403, 429, 500, 502, 503, 504):
                log.warning("SEC returned %s, backing off: %s", r.status_code, url)
                time.sleep(2 ** attempt * 2)
                continue
            r.raise_for_status()
            return r.content if binary else r.text
        raise RuntimeError(f"SEC request kept failing: {url}")


def _value(el, path):
    node = el.find(path) if el is not None else None
    if node is None:
        return None
    inner = node.find("value")
    text = inner.text if inner is not None else node.text
    return text.strip() if text and text.strip() else None


def _flag(text) -> bool:
    return str(text).strip().lower() in ("1", "true")


def _num(text) -> float:
    try:
        return float(str(text).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def parse_form4(xml: str, filing_date: date, accession: str = "") -> list[dict]:
    """Returns the open-market purchase rows of one Form 4 document."""
    root = ET.fromstring(xml)
    ticker = (_value(root, "issuer/issuerTradingSymbol") or "").upper().strip()
    issuer_cik = _value(root, "issuer/issuerCik") or ""
    # Joint filings list several owners for the same shares; count only the first
    # so one purchase is not counted twice.
    owner = root.find("reportingOwner")
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    base = {
        "ticker": ticker,
        "issuer_cik": issuer_cik.lstrip("0"),
        "owner_cik": (_value(owner, "reportingOwnerId/rptOwnerCik") or "").lstrip("0"),
        "owner_name": _value(owner, "reportingOwnerId/rptOwnerName") or "",
        "is_director": _flag(_value(rel, "isDirector")),
        "is_officer": _flag(_value(rel, "isOfficer")),
        "is_ten_pct": _flag(_value(rel, "isTenPercentOwner")),
        "officer_title": _value(rel, "officerTitle") or "",
        "filing_date": filing_date.isoformat(),
        "accession": accession,
    }
    rows = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _value(t, "transactionCoding/transactionCode")
        if code != "P":
            continue
        shares = _num(_value(t, "transactionAmounts/transactionShares"))
        price = _num(_value(t, "transactionAmounts/transactionPricePerShare"))
        rows.append({
            **base,
            "trans_date": (_value(t, "transactionDate") or base["filing_date"])[:10],
            "code": code,
            "acq_disp": _value(t, "transactionAmounts/transactionAcquiredDisposedCode") or "A",
            "shares": shares,
            "price": price,
            "value": shares * price,
        })
    return rows


def _form4_filenames(client: SecClient, day: date) -> list[str] | None:
    """Form 4 filings listed in the EDGAR daily index, or None if there is no index for that day."""
    q = (day.month - 1) // 3 + 1
    text = client.get(f"{SEC}/Archives/edgar/daily-index/{day.year}/QTR{q}/form.{day:%Y%m%d}.idx")
    if text is None:
        return None
    names = set()
    for line in text.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        # Each filing is listed under both the company and the insider; the set de-duplicates.
        if len(parts) >= 5 and parts[0] == "4":
            names.add(parts[-1])
    return sorted(names)


def _extract_xml(submission: str) -> str | None:
    m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", submission, re.S)
    return m.group(0) if m else None


def fetch_day(client: SecClient, day: date, cache_dir: Path) -> list[dict]:
    """All purchase rows filed on one day. Finished days are cached on disk."""
    cache = cache_dir / "form4_days" / f"{day:%Y%m%d}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    names = _form4_filenames(client, day)
    if names is None:
        # Weekend/holiday, or today's index is not published yet. Only remember
        # "no filings" for days old enough that the index should exist.
        if day <= date.today() - timedelta(days=3):
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text("[]", encoding="utf-8")
        return []
    log.info("SEC %s: %d Form 4 filings to read", day, len(names))
    rows = []
    for i, name in enumerate(names, 1):
        try:
            submission = client.get(f"{SEC}/Archives/{name}")
            xml = _extract_xml(submission or "")
            if xml:
                rows.extend(parse_form4(xml, day, Path(name).stem))
        except ET.ParseError as e:
            log.debug("Skipping unparsable filing %s: %s", name, e)
        if i % 250 == 0:
            log.info("  ... %d/%d", i, len(names))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows), encoding="utf-8")
    return rows


def recent_purchases(client: SecClient, days: int, cache_dir: Path, today: date | None = None) -> pd.DataFrame:
    """Insider purchases filed in the last `days` calendar days (weekdays only)."""
    today = today or date.today()
    rows = []
    for back in range(days, -1, -1):
        day = today - timedelta(days=back)
        if day.weekday() < 5:
            rows.extend(fetch_day(client, day, cache_dir))
    return to_frame(rows)


def to_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["trans_date"] = pd.to_datetime(df["trans_date"], errors="coerce")
    df["filing_date"] = pd.to_datetime(df["filing_date"], errors="coerce")
    for col in ("is_director", "is_officer", "is_ten_pct"):
        df[col] = df[col].astype(bool)
    return df.dropna(subset=["trans_date", "filing_date"])
