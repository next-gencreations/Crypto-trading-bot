
from decimal import Decimal, getcontext
from datetime import datetime, timezone, date
import time
import csv
import requests
import os
import random

# Higher precision for crypto maths
getcontext().prec = 28

# ============================================================
# CONFIG
# ============================================================

# Starting balance (from env or default 100)
START_BALANCE_USD = Decimal(os.getenv("START_BALANCE_USD", "100"))

# Risk mode: SAFE or AGGRESSIVE
RISK_MODE = os.getenv("RISK_MODE", "SAFE").upper()

# Big universe of possible USD markets to sample from
ALL_MARKETS = [
    "BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "ADA-USD",
    "LTC-USD", "DOGE-USD", "LINK-USD", "MATIC-USD", "OP-USD",
    "ARB-USD", "ATOM-USD", "SAND-USD", "UNI-USD", "RNDR-USD",
]

# How many random markets to scan each cycle
MAX_MARKETS_PER_SCAN = 8

# Time settings
SLEEP_SECONDS = 6 * 60           # 6 minutes
CANDLE_GRANULARITY = 300         # 5-minute candles
LOOKBACK_CANDLES = 100           # how many candles for indicators

# ---- Base (SAFE) risk parameters ----
TAKE_PROFIT_PCT = Decimal("0.010")       # +1.0%
STOP_LOSS_PCT = Decimal("-0.015")        # -1.5%
POSITION_SIZE_FRACTION = Decimal("0.3")  # 30% of USD per new trade
MAX_OPEN_POSITIONS = 1                   # one position at a time

MIN_TREND_STRENGTH = Decimal("0.002")    # short MA > long MA by 0.2%
RSI_BUY_MIN = Decimal("40")
RSI_BUY_MAX = Decimal("65")

MIN_VOLATILITY = Decimal("0.002")        # 0.2% avg move per candle
MAX_VOLATILITY = Decimal("0.03")         # 3% avg move per candle

MAX_DAILY_DRAWDOWN = Decimal("0.05")     # 5% daily drawdown limit
MAX_LOSING_STREAK = 3                    # after 3 losses, pause

# ---- Override for AGGRESSIVE mode ----
if RISK_MODE == "AGGRESSIVE":
    TAKE_PROFIT_PCT = Decimal("0.020")       # +2.0%
    STOP_LOSS_PCT = Decimal("-0.03")         # -3.0%
    POSITION_SIZE_FRACTION = Decimal("0.5")  # 50% of USD
    MAX_OPEN_POSITIONS = 3                   # up to 3 coins at once
    MIN_TREND_STRENGTH = Decimal("0.0015")   # slightly weaker trend allowed
    RSI_BUY_MIN = Decimal("35")
    RSI_BUY_MAX = Decimal("70")
    MAX_DAILY_DRAWDOWN = Decimal("0.08")     # allow up to 8% daily loss

PUBLIC_API_BASE = "https://api.exchange.coinbase.com"
TRADE_LOG = "live_sim_trade_history.csv"

# ============================================================
# STATE
# ============================================================

usd_balance = START_BALANCE_USD

# list of open positions:
# {
#   "market": "BTC-USD",
#   "amount": Decimal,
#   "entry_price": Decimal,
#   "entry_time": datetime
# }
positions = []

start_time = datetime.now(timezone.utc)
trade_count = 0
losing_streak = 0

equity_peak_today = START_BALANCE_USD
today = date.today()
trading_paused_for_today = False  # due to daily drawdown

# Create trade log if needed
if not os.path.exists(TRADE_LOG):
    with open(TRADE_LOG, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Timestamp",
            "Action",
            "Market",
            "Price",
            "Amount",
            "USD_Balance",
            "Position_Count",
            "Equity_Value",
            "Profit_Loss"
        ])

# ============================================================
# HELPERS
# ============================================================

def log(msg: str) -> None:
    """Simple timestamped logger."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} | {msg}", flush=True)


def get_candles(market: str, limit: int = LOOKBACK_CANDLES):
    """Fetch recent candles for a market."""
    end_time = int(time.time())
    start_time = end_time - (limit * CANDLE_GRANULARITY)

    start_iso = datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat()

    url = f"{PUBLIC_API_BASE}/products/{market}/candles"
    params = {
        "start": start_iso,
        "end": end_iso,
        "granularity": CANDLE_GRANULARITY
    }

    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    candles = resp.json()
    # Coinbase returns [time, low, high, open, close, volume]
    if not candles:
        return []
    candles.sort(key=lambda c: c[0])
    return candles


def get_latest_price(market: str) -> Decimal | None:
    """Fetch latest price for a market."""
    url = f"{PUBLIC_API_BASE}/products/{market}/ticker"
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        return None
    data = resp.json()
    return Decimal(str(data["price"]))


def sma(values, period: int) -> Decimal | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / Decimal(period)


def rsi(values, period: int = 14) -> Decimal | None:
    if len(values) <= period:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = values[-i] - values[-i - 1]
        if diff > 0:
            gains.append(diff)
        else:
            losses.append(-diff)
    if not gains and not losses:
        return Decimal(50)
    avg_gain = sum(gains) / Decimal(period) if gains else Decimal(0)
    avg_loss = sum(losses) / Decimal(period) if losses else Decimal(0)
    if avg_loss == 0:
        return Decimal(100)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def volatility(values) -> Decimal | None:
    """Average absolute % change per candle."""
    if len(values) < 2:
        return None
    moves = []
    for i in range(1, len(values)):
        if values[i - 1] == 0:
            continue
        pct = (values[i] - values[i - 1]) / values[i - 1]
        moves.append(abs(pct))
    if not moves:
        return None
    return sum(moves) / Decimal(len(moves))


def score_market(market: str):
    """
    Return a score for this market based on trend, RSI, volatility.
    Higher = more attractive. If unsuitable, score -999.
    """
    try:
        candles = get_candles(market)
    except Exception as e:
        log(f"Error fetching candles for {market}: {e}")
        return Decimal("-999"), None, None

    if not candles or len(candles) < 30:
        return Decimal("-999"), None, None

    closes = [Decimal(str(c[4])) for c in candles]

    short_ma = sma(closes, 9)
    long_ma = sma(closes, 21)
    current_rsi = rsi(closes, 14)
    vol = volatility(closes)

    if short_ma is None or long_ma is None or current_rsi is None or vol is None:
        return Decimal("-999"), None, None

    trend = (short_ma - long_ma) / long_ma

    # Filters – must all pass
    if trend <= MIN_TREND_STRENGTH:
        return Decimal("-999"), None, None
    if not (RSI_BUY_MIN <= current_rsi <= RSI_BUY_MAX):
        return Decimal("-999"), None, None
    if not (MIN_VOLATILITY <= vol <= MAX_VOLATILITY):
        return Decimal("-999"), None, None

    # Simple score: stronger trend & healthy volatility
    score = trend * Decimal("1000") + (MAX_VOLATILITY - vol) * Decimal("10")
    price_now = closes[-1]
    return score, price_now, closes


def get_random_scan_list():
    """Pick a random subset of ALL_MARKETS to scan this cycle."""
    n = min(MAX_MARKETS_PER_SCAN, len(ALL_MARKETS))
    return random.sample(ALL_MARKETS, k=n)


def log_trade(action, market, price, amount, equity_value, profit_loss):
    global usd_balance, positions
    with open(TRADE_LOG, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            action,
            market,
            f"{price:.8f}",
            f"{amount:.8f}",
            f"{usd_balance:.2f}",
            len(positions),
            f"{equity_value:.2f}",
            f"{profit_loss:.2f}",
        ])


def current_equity():
    """USD + value of all open positions at latest prices."""
    total = usd_balance
    for pos in positions:
        price = get_latest_price(pos["market"])
        if price is None:
            continue
        total += pos["amount"] * price
    return total


def choose_best_market():
    """
    Scan a random subset of markets and return the best candidate
    that we are NOT already holding.
    """
    scan_list = get_random_scan_list()
    log(f"Scanning {len(scan_list)} random markets this cycle...")

    # Don't re-buy markets we already hold
    held_markets = {p["market"] for p in positions}

    best = (Decimal("-999"), None, None, None)  # score, market, price, closes
    for m in scan_list:
        if m in held_markets:
            continue
        score, price, closes = score_market(m)
        log(f"Market {m} score {score:.4f}")
        if price is None:
            continue
        if score > best[0]:
            best = (score, m, price, closes)

    if best[1] is None or best[0] <= Decimal("-999"):
        log("No suitable market found this cycle.")
        return None, None

    log(f"Best candidate this cycle: {best[1]} with score {best[0]:.4f}")
    return best[1], best[2]


def close_position(pos, reason: str):
    """
    Close a position at current market price.
    Updates usd_balance, trade logs, equity, losing streak.
    """
    global usd_balance, trade_count, losing_streak, equity_peak_today

    price = get_latest_price(pos["market"])
    if price is None:
        log(f"Could not fetch price to close {pos['market']}. Skipping.")
        return

    entry_value = pos["amount"] * pos["entry_price"]
    exit_value = pos["amount"] * price
    profit_loss = exit_value - entry_value

    usd_balance += exit_value
    trade_count += 1
    losing_streak = losing_streak + 1 if profit_loss < 0 else 0

    equity = current_equity()
    if equity > equity_peak_today:
        equity_peak_today = equity

    log(
        f"CLOSE {pos['market']} @ {price:.2f} "
        f"({reason}) | P/L: {profit_loss:+.2f} | Equity ≈ ${equity:.2f}"
    )
    log_trade(
        action=f"SELL_{reason}",
        market=pos["market"],
        price=price,
        amount=pos["amount"],
        equity_value=equity,
        profit_loss=profit_loss,
    )


# ============================================================
# MAIN LOOP
# ============================================================

def main_loop():
    global usd_balance, positions, equity_peak_today, today, trading_paused_for_today, losing_streak

    log("============================================================")
    log("CRYPTO PAPER-TRADING BOT (RANDOM MULTI-MARKET SCAN)")
    log("NO REAL MONEY. NO REAL ORDERS. PUBLIC DATA ONLY.")
    log(f"Starting balance: ${START_BALANCE_USD}")
    log(f"Risk mode: {RISK_MODE}")
    log("============================================================")

    while True:
        try:
            now = datetime.now(timezone.utc)

            # Reset daily drawdown tracking at start of new UTC day
            if now.date() != today:
                today = now.date()
                equity_peak_today = current_equity()
                trading_paused_for_today = False
                losing_streak = 0
                log("----- New day: resetting daily stats (peak equity, losing streak, pause flag) -----")

            # 1) Update existing positions (check TP/SL)
            still_open = []
            for pos in positions:
                price = get_latest_price(pos["market"])
                if price is None:
                    still_open.append(pos)
                    continue

                change_pct = (price - pos["entry_price"]) / pos["entry_price"]

                if change_pct >= TAKE_PROFIT_PCT:
                    close_position(pos, "TAKE_PROFIT")
                elif change_pct <= STOP_LOSS_PCT:
                    close_position(pos, "STOP_LOSS")
                else:
                    still_open.append(pos)

            positions = still_open

            # 2) Compute current equity & check drawdown
            equity = current_equity()
            if equity > equity_peak_today:
                equity_peak_today = equity

            dd = (equity_peak_today - equity) / equity_peak_today if equity_peak_today > 0 else Decimal(0)

            if dd >= MAX_DAILY_DRAWDOWN:
                trading_paused_for_today = True

            # 3) Log a quick summary every loop
            log(
                f"Summary: USD=${usd_balance:.2f}, positions={len(positions)}, "
                f"Equity≈${equity:.2f}, DD={dd * 100:.2f}%, LosingStreak={losing_streak}"
            )

            # 4) Risk-based pause checks
            if trading_paused_for_today:
                log("Daily drawdown limit hit. Pausing new entries for the rest of the day.")
            elif losing_streak >= MAX_LOSING_STREAK:
                log(f"Losing streak {losing_streak} ≥ {MAX_LOSING_STREAK}. Pausing new entries this cycle.")
            elif len(positions) >= MAX_OPEN_POSITIONS:
                log(f"Max open positions ({MAX_OPEN_POSITIONS}) reached. Not opening new trades this cycle.")
            else:
                # 5) Look for a new entry
                market, price = choose_best_market()
                if market and price:
                    # position sizing
                    usd_to_spend = (usd_balance * POSITION_SIZE_FRACTION).quantize(Decimal("0.01"))
                    if usd_to_spend > Decimal("5"):   # min trade size (sim only)
                        amount = (usd_to_spend / price).quantize(Decimal("0.00000001"))
                        usd_balance -= usd_to_spend
                        pos = {
                            "market": market,
                            "amount": amount,
                            "entry_price": price,
                            "entry_time": now
                        }
                        positions.append(pos)
                        entry_equity = current_equity()
                        log(
                            f"OPEN {market} @ {price:.2f} | "
                            f"Spend=${usd_to_spend:.2f}, Amount={amount:.8f}, "
                            f"Positions now={len(positions)}"
                        )
                        log_trade(
                            action="BUY",
                            market=market,
                            price=price,
                            amount=amount,
                            equity_value=entry_equity,
                            profit_loss=Decimal("0.00"),
                        )
                    else:
                        log(f"Not enough USD to open new trade (would spend ${usd_to_spend:.2f}).")

            # 6) Sleep until next cycle
            log(f"Sleeping for {SLEEP_SECONDS} seconds...\n")
            time.sleep(SLEEP_SECONDS)

        except Exception as e:
            log(f"Error in main loop: {e}")
            # short sleep to avoid tight crash loop
            time.sleep(10)


if __name__ == "__main__":
    log("Starting Crypto-trading-bot main loop...")
    main_loop()
