# Claude training: insider-buying trading bot

A small experiment: a bot that buys US stocks after company insiders buy their own
company's shares, starting with **paper trading** (fake money) on Alpaca.

> **Expectations.** The budget is 20 PLN (~5.50 USD). A good month for any honest
> strategy is a few percent, which is cents here. This is a test of whether the
> strategy works, not a way to make real money yet. All trading carries risk of loss.

## How it works

Every weekday at 16:30 (Polish time, US market open) the bot:

1. **Reads SEC Form 4 filings** (official, free). US company insiders must report
   their stock trades within 2 business days. Only open-market *purchases* count.
2. **Finds signals:**
   - a **cluster**: 2+ different officers/directors bought within 14 days, together ≥ $100k, or
   - the **CEO or CFO alone** bought ≥ $250k.
   Big funds (10% owners) and penny stocks are ignored. Signals are ranked by size,
   number of insiders, and CEO/CFO involvement.
3. **Sells** a position after 60 days, or if it falls 15% below its highest close
   (trailing stop).
4. **Buys** the best new signals, up to 4 stocks, splitting the budget equally
   (fractional shares, minimum $1 per order).
5. **Emergency stop**: if the bot's money drops 25% below the start, it sells everything
   and stops trading.
6. **Emails** a daily report: money, positions, actions, top signals.

All settings are in [`config.yaml`](config.yaml).

Congress trades are not used yet: they are reported up to 45 days late, and the free
data sources are unreliable. They can be added later as a second signal.

## Setup on Windows (about 15 minutes)

You need Python 3.10 or newer (check with `py --version` in a terminal).

1. **Download the code.** On GitHub press *Code → Download ZIP* and unzip it, e.g. to
   `C:\claude-training`. (Or `git clone` if you use git.)
2. **Install.** Double-click `setup.bat`. Wait until it says *Setup done*.
3. **Fill in your keys.** Open `.env` in Notepad (`notepad .env` in that folder) and fill in:
   - `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`: Alpaca → **Paper** account → API Keys
   - `SEC_USER_AGENT`: e.g. `claude-training-bot youremail@gmail.com` (the SEC requires a contact email)
   - `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `REPORT_TO`: the Gmail that sends the report, its
     16-letter app password, and the address that receives it

   Never share this file or upload it anywhere.
4. **Test the connection.** Open a terminal in the folder and run:
   ```
   run_bot.bat --check
   ```
   It should print your Alpaca paper account and send you a test email.
5. **Dry run.** See what the bot *would* do, without placing orders:
   ```
   run_bot.bat --dry-run
   ```
   The **first run takes 20-40 minutes** because it downloads ~2 weeks of SEC filings.
   Later runs only fetch new days and take a few minutes.
6. **Backtest (optional but recommended).** Test the strategy on past years:
   ```
   .venv\Scripts\python -m backtest.backtest --start 2022-01-01
   ```
   The first time it downloads SEC data and prices, which takes a while. Results go to
   `backtest\results\`.
7. **Turn on the daily schedule.** Double-click `install_schedule.bat`. The PC must be
   on (it may sleep; the task runs when it wakes) around 16:30 on weekdays.

## Daily use

- Read the email. Logs are in `logs\bot.log`; the bot's records are in `state.json`.
- Don't trade by hand in the same Alpaca paper account: the bot assumes it owns it.
- **Paper account size:** the paper account starts with $100,000, but the bot only uses
  `capital_usd` from `config.yaml` (5.50).

## Kill switch / starting over

If the emergency stop triggers, the bot stops trading. To start fresh, delete
`state.json` (the bot then starts again with `capital_usd`). Only do this after
checking why it lost money.

## Going live later

Not before 2-4 weeks of sensible paper results. Funding Alpaca from Poland costs an
international transfer fee that is larger than 20 PLN, so live trading will likely use a
different broker (e.g. Trading 212) and needs a new connector. Ask Claude to add it.

## Development

```
.venv\Scripts\python -m pytest
```
