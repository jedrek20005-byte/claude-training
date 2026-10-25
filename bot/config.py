"""Loads config.yaml (settings) and .env (secrets)."""
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
STATE_FILE = ROOT / "state.json"


def load_config(path: Path | None = None) -> dict:
    with open(path or ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class Secrets:
    alpaca_key: str
    alpaca_secret: str
    alpaca_paper: bool
    sec_user_agent: str
    gmail_address: str
    gmail_app_password: str
    report_to: str


def load_secrets() -> Secrets:
    load_dotenv(ROOT / ".env")
    s = Secrets(
        alpaca_key=os.getenv("ALPACA_API_KEY", "").strip(),
        alpaca_secret=os.getenv("ALPACA_SECRET_KEY", "").strip(),
        alpaca_paper=os.getenv("ALPACA_PAPER", "true").strip().lower() != "false",
        sec_user_agent=os.getenv("SEC_USER_AGENT", "").strip(),
        gmail_address=os.getenv("GMAIL_ADDRESS", "").strip(),
        gmail_app_password=os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", ""),
        report_to=os.getenv("REPORT_TO", "").strip(),
    )
    missing = [n for n, v in [("ALPACA_API_KEY", s.alpaca_key), ("ALPACA_SECRET_KEY", s.alpaca_secret),
                              ("SEC_USER_AGENT", s.sec_user_agent)] if not v]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)} (see .env.example)")
    return s
