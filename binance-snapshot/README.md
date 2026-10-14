# Binance Spot snapshot

This standalone, read-only analyzer uses its own virtual environment. Its packages are
declared here, independently of the parent trading advisor application.

From `binance-snapshot/`, set up Python 3.13 and install runtime dependencies:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

For optional Telegram delivery, put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
in the project-local `.env` as `KEY=value` lines, and keep the file private.
The root `.gitignore` excludes `.env`. Alternatively, provide both variables
through the process environment. A complete environment pair takes precedence
over `.env`; if both variables are absent, `.env` is used. A missing, empty, or
invalid environment value fails without mixing the two sources. Library calls
with an explicit `load_config(path)` always use that file. Configuration is
loaded only when a sending command reaches the delivery step.

Run the manual snapshot with `.venv/bin/python snapshot.py`. It fetches public Binance
Spot market data; no API key is required.

Binance requests use `https://api.binance.com` by default. To select Binance's
official public market-data endpoint for a process, set
`BINANCE_API_BASE_URL=https://data-api.binance.vision` in its environment. The
only accepted values are those two exact HTTPS base URLs. The selected host is
used for server time, 24-hour tickers, and klines; the hourly runner keeps one
selection for its entire invocation. An invalid value fails before any Binance
request or Telegram delivery. There is no automatic fallback after an HTTP 451
or any other failure. Endpoint availability and permission to use it in a
deployment location must be confirmed independently.

To install test tools and run the offline regression suite:

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

The tests mock HTTP requests and do not contact Binance. Run these commands from this
directory so imports and the relative requirements path resolve correctly.

## Optional Telegram snapshot

The existing `snapshot.py` command still prints its report and never sends Telegram
messages. To explicitly collect and send one complete snapshot, run:

```sh
.venv/bin/python telegram_snapshot.py --send
```

The command uses the existing notifier and loads Telegram configuration only
after a valid snapshot has been collected and formatted. It prints a short
success or failure status without credentials. Running `telegram_snapshot.py`
without `--send` makes no network requests and sends nothing.

The Telegram report is plain text. It lists the six tracked assets in a fixed
order, labels ticker prices as live, and computes EMA20/50, RSI14, ATR14 and
RVOL20 only from closed 1H and 4H candles. Unavailable indicators are labeled
`unavailable`. The formatter rejects a report that exceeds Telegram's
one-message text limit rather than truncating it. Failed or uncertain delivery
is not retried automatically. The manual command remains independent of the
hourly runner below.

## Optional hourly Telegram snapshot on macOS

`hourly_snapshot.py` is a separate entry point. It uses the same structured
`MarketSnapshot`, Telegram formatter, and notifier. At most one message is attempted
per invocation. It checks the public Binance Spot server clock before collection
and again immediately before delivery. The fully closed, common 1H candle must be
the latest expected candle and be 2–15 minutes old. All six assets must have
aligned closed 1H and 4H candles, enough candles for EMA50, and fresh ticker
prices. If a scheduled run wakes late, falls outside the window, or overlaps
another run, it skips sending. An invalid or incomplete snapshot fails without
sending. No trading API is used.

Read-only manual verification, including Binance public market data requests
but no `.env` read or Telegram delivery:

```sh
cd /Users/konstantinklinov/Developer/advancedTradingAdvisor/binance-snapshot
.venv/bin/python hourly_snapshot.py --check
```

This succeeds with an eligibility message during the window, or a skip message
outside it. To send one eligible snapshot manually, run
`.venv/bin/python hourly_snapshot.py` **only when you intend to send Telegram**.
It loads Telegram configuration after collection. The notifier attempts one
`sendMessage` request; uncertain delivery is not retried.

### Prepare and install the LaunchAgent

The checked-in template is
`launchd/com.konstantinklinov.binance-snapshot.hourly.plist`. It uses the
absolute `.venv/bin/python` and script paths above. Set up the virtual
environment first using the commands at the top of this README. The template
is prepared only; installing or activating it is a separate manual action:

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp /Users/konstantinklinov/Developer/advancedTradingAdvisor/binance-snapshot/launchd/com.konstantinklinov.binance-snapshot.hourly.plist "$HOME/Library/LaunchAgents/"
plutil -lint "$HOME/Library/LaunchAgents/com.konstantinklinov.binance-snapshot.hourly.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.konstantinklinov.binance-snapshot.hourly.plist"
```

It schedules at local minute 07 of every hour while the user is logged in. It
has neither `RunAtLoad` nor `KeepAlive`. The computer must be running; this
configuration does not wake it. Apple's `StartCalendarInterval` can start a
missed run when the Mac wakes from sleep (missed sleep events are coalesced).
The freshness gate rejects that delayed run if the latest hourly candle is
outside its 2–15 minute window. Hours missed while the Mac is powered off are
not replayed. Local clock changes and time zone changes can affect the
scheduled minute; the Binance clock and UTC candle checks determine eligibility.

To inspect the agent and its local logs:

```sh
launchctl print "gui/$(id -u)/com.konstantinklinov.binance-snapshot.hourly"
tail -n 50 "$HOME/Library/Logs/binance-snapshot-hourly.out.log"
tail -n 50 "$HOME/Library/Logs/binance-snapshot-hourly.err.log"
```

The runner logs only short delivery, skip, or failure statuses. It does not log
credentials, Telegram response bodies, or token-bearing URLs. If it fails,
verify the project `.venv`, the installed plist paths, network access, and the
local `.env` values without posting those values into logs or support requests.
If it skips, check the run time relative to the latest UTC hourly close.

To remove the agent after installation:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.konstantinklinov.binance-snapshot.hourly.plist"
rm "$HOME/Library/LaunchAgents/com.konstantinklinov.binance-snapshot.hourly.plist"
```

The lock file is under `~/Library/Application Support/binance-snapshot/`. It
is only an OS advisory lock; the file can remain after a crash and must not be
deleted to recover. The OS releases the lock when the process exits. This
prevents simultaneous runs, but sequential runs in the same hour can send
duplicates. There is no persistent delivery state or automatic Telegram delivery
retry. Read-only Binance GETs have at most two attempts under an overall deadline.
Sleep after the final freshness check or an uncertain Telegram response can still
affect delivery timing. No automatic trading occurs.
