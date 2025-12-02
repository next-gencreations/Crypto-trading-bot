from decimal import Decimal, getcontext
from datetime import datetime, timezone, date
import time
import csv
import requests
import os
import random

# ===========================
# High precision for crypto math
# ===========================
getcontext().prec = 28

# ===========================
# CONFIG
# ===========================

# Start balance – can be overridden via env var START_BALANCE_USD
START_BALANCE_USD = Decimal(
    os.getenv("START_BALANCE_USD", "100").strip()
)

# Big universe of possible USD markets to sample from
ALL_MARKETS = [
    "BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "ADA-USD",
    "LTC-USD", "DOGE-USD", "LINK-USD", "MATIC-USD", "OP-USD",
    "ARB-USD", "ATOM-USD", "SAND-USD", "UNI-USD", "RNDR-USD",
]

# How many random markets to scan each cycle
MAX_MARKETS_PER_SCAN = 8

# Loop timing
SLEEP_SECONDS = 6 * 60            # run roughly every 6 minutes
CANDLE_GRANULARITY = 300          # 5-minute candles
LOOKBACK_CANDLES = 100            # how many candles for indicators

# Trading / risk parameters
TAKE_PROFIT_PCT = Decimal("0.010")        # +1.0%
STOP_LOSS_PCT = Decimal("0.015")          # -1.5%
POSITION_SIZE_FRACTION = Decimal("0.30")  # 30% of cash per trade

MIN_TREND_STRENGTH = Decimal("0.002")     # short MA must be > long MA by 0.2%
RSI_BUY_MIN = Decimal("40")              # oversold-ish
RSI_BUY_MAX = Decimal("65")              # not too overbought

MIN_VOLATILITY = Decimal("0.002")        # 0.2% avg move per candle
MAX_VOLATILITY = Decimal("0.030")        # 3% avg move per candle

MAX_DAILY_DRAWDOWN = Decimal("0.05")     # 5% daily drawdown limit
MAX_LOSING_STREAK = 3                    # after 3 losses, pause for a while

PUBLIC_API_BASE = "https://api.exchange.coinbase.com"
TRADE_LOG = "live_sim_trade_history.csv"

# ===========================
# GLOBAL STATE
# ===========================

usd_balance = START_BALANCE_USD
coin_balance = Decimal("0")
current_market = None
entry_price = None
trade_count = 0
losing_streak = 0

equity_peak_today = START_BALANCE_USD
today_date = date.today()
trading_paused_for_today = False  # triggered by daily drawdown

# ===========================
# Utilities
# ===========================

def log(msg: str) -> None:
    """Print a timestamped log line."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def ensure_trade_log_exists() -> None:
    """Create CSV with header if it doesn't exist yet."""
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "event",
                "market",
                "price",
                "amount",
                "usd_balance",
                "coin_balance",
                "equity",
                "pnl",
                "comment",
            ])


def log_event(event: str,
              market: str,
              price: Decimal,
              amount: Decimal,
              comment: str = "") -> None:
    """Append an event row to the CSV log."""
    ensure_trade_log_exists()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    equity = current_equity(price)
    pnl = equity - START_BALANCE_USD
    with open(TRADE_LOG, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            ts,
            event,
            market or "",
            f"{price:.8f}",
            f"{amount:.8f}",
            f"{usd_balance:.2f}",
            f"{coin_balance:.8f}",
            f"{equity:.2f}",
            f"{pnl:.2f}",
            comment,
        ])


# ===========================
# Market data helpers
# ===========================

def get_latest_price(market: str) -> Decimal | None:
    """Fetch latest price for a market using Coinbase public API."""
    url = f"{PUBLIC_API_BASE}/products/{market}/ticker"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return Decimal(str(data["price"]))
    except Exception as e:
        log(f"Error fetching latest price for {market}: {e}")
        return None


def get_recent_candles(market: str, limit: int = LOOKBACK_CANDLES) -> list[Decimal] | None:
    """
    Fetch recent candles using Coinbase public API.
    Returns list of close prices as Decimals (oldest -> newest).
    """
    now_ts = int(time.time())
    start_ts = now_ts - limit * CANDLE_GRANULARITY

    start_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()

    url = f"{PUBLIC_API_BASE}/products/{market}/candles"
    params = {
        "start": start_iso,
        "end": end_iso,
        "granularity": CANDLE_GRANULARITY,
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        candles = resp.json()
        # candle format: [time, low, high, open, close, volume]
        closes = [Decimal(str(c[4])) for c in candles]
        closes.reverse()  # chronological
        if len(closes) < 30:
            return None
        return closes
    except Exception as e:
        log(f"Error fetching candles for {market}: {e}")
        return None


# ===========================
# Indicators
# ===========================

def sma(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / Decimal(period)


def rsi(values: list[Decimal], period: int = 14) -> Decimal | None:
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
        return Decimal("50")
    avg_gain = sum(gains) / Decimal(period) if gains else Decimal(0)
    avg_loss = sum(losses) / Decimal(period) if losses else Decimal(0)
    if avg_loss == 0:
        return Decimal("100")
    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def avg_abs_return(values: list[Decimal]) -> Decimal:
    """Average absolute % move per candle."""
    if len(values) < 2:
        return Decimal(0)
    total = Decimal(0)
    for i in range(1, len(values)):
        prev = values[i - 1]
        curr = values[i]
        if prev <= 0:
            continue
        total += abs((curr - prev) / prev)
    return total / Decimal(len(values) - 1)


# ===========================
# Scoring / selection
# ===========================

def get_random_scan_list() -> list[str]:
    """Pick a random subset of ALL_MARKETS to scan this cycle."""
    n = min(MAX_MARKETS_PER_SCAN, len(ALL_MARKETS))
    return random.sample(ALL_MARKETS, k=n)


def score_market(market: str) -> tuple[Decimal, Decimal | None, list[Decimal] | None]:
    """
    Score a market.
    Returns (score, latest_price, closes) where score < 0 means "reject".
    """
    closes = get_recent_candles(market)
    if not closes:
        return (Decimal("-999"), None, None)

    price = closes[-1]

    short_ma = sma(closes, 9)
    long_ma = sma(closes, 21)
    current_rsi = rsi(closes, 14)
    vol = avg_abs_return(closes[-40:])  # last 40 candles

    if short_ma is None or long_ma is None or current_rsi is None:
        return (Decimal("-999"), None, None)

    # Trend filter
    trend_strength = (short_ma - long_ma) / long_ma
    if trend_strength < MIN_TREND_STRENGTH:
        return (Decimal("-999"), price, closes)

    # RSI filter
    if not (RSI_BUY_MIN <= current_rsi <= RSI_BUY_MAX):
        return (Decimal("-999"), price, closes)

    # Volatility filter
    if not (MIN_VOLATILITY <= vol <= MAX_VOLATILITY):
        return (Decimal("-999"), price, closes)

    # Basic score: stronger trend + closer RSI to (RSI_BUY_MIN + RSI_BUY_MAX)/2
    rsi_mid = (RSI_BUY_MIN + RSI_BUY_MAX) / 2
    rsi_score = Decimal("1") - (abs(current_rsi - rsi_mid) / Decimal("50"))
    vol_score = Decimal("1") - abs(vol - (MIN_VOLATILITY + MAX_VOLATILITY) / 2)

    score = trend_strength * Decimal("100") + rsi_score + vol_score
    return (score, price, closes)


def choose_best_market() -> tuple[Decimal, str | None, Decimal | None, list[Decimal] | None]:
    """
    Randomly sample some markets from ALL_MARKETS,
    score them, and return the best one.
    """
    best = (Decimal("-999"), None, None, None)  # score, market, price, closes
    scan_list = get_random_scan_list()

    log(f"Scanning {len(scan_list)} random markets this cycle...")

    for m in scan_list:
        score, price, closes = score_market(m)
        if price is None:
            continue
        log(f"Market {m} score {score:.4f}")
        if score > best[0]:
            best = (score, m, price, closes)

    return best  # (score, market, price, closes)


# ===========================
# Risk & equity
# ===========================

def current_equity(last_price: Decimal | None) -> Decimal:
    if last_price is None or coin_balance == 0:
        return usd_balance
    return usd_balance + coin_balance * last_price


def update_daily_state(last_price: Decimal | None) -> None:
    """Update drawdown + daily reset logic."""
    global equity_peak_today, today_date, trading_paused_for_today

    now = datetime.now(timezone.utc)
    d = now.date()

    # New day: reset counters
    if d != today_date:
        log("New day detected – resetting daily stats.")
        today_date = d
        equity_peak_today = current_equity(last_price or Decimal("0"))
        trading_paused_for_today = False

    equity = current_equity(last_price or Decimal("0"))
    if equity > equity_peak_today:
        equity_peak_today = equity

    if equity_peak_today <= 0:
        return

    drawdown = (equity_peak_today - equity) / equity_peak_today

    if drawdown >= MAX_DAILY_DRAWDOWN and not trading_paused_for_today:
        trading_paused_for_today = True
        log(f"Daily drawdown {drawdown:.3%} exceeded limit "
            f"{MAX_DAILY_DRAWDOWN:.1%} – pausing new trades for today.")
        log_event("DAILY_DRAWDOWN_LOCK", current_market or "", last_price or Decimal("0"),
                  Decimal("0"), "Daily drawdown lockout")


# ===========================
# Trading actions
# ===========================

def enter_position(market: str, price: Decimal) -> None:
    """Buy into a new position."""
    global usd_balance, coin_balance, current_market, entry_price, trade_count

    if usd_balance <= 0:
        return

    usd_to_spend = (usd_balance * POSITION_SIZE_FRACTION).quantize(Decimal("0.01"))
    if usd_to_spend < Decimal("10"):
        log("Not enough USD to enter a position.")
        return

    coins = (usd_to_spend / price).quantize(Decimal("0.00000001"))
    if coins <= 0:
        log("Calculated coin amount is zero; skipping trade.")
        return

    usd_balance -= usd_to_spend
    coin_balance += coins
    current_market = market
    entry_price = price
    trade_count += 1

    log(f"BUY {coins} of {market} @ {price} (spend ${usd_to_spend})")
    log_event("BUY", market, price, coins, "Enter position")


def exit_position(reason: str, price: Decimal) -> None:
    """Sell current position."""
    global usd_balance, coin_balance, current_market, entry_price, losing_streak

    if current_market is None or entry_price is None or coin_balance <= 0:
        return

    coins = coin_balance
    proceeds = (coins * price).quantize(Decimal("0.01"))
    usd_balance += proceeds
    coin_balance = Decimal("0")

    pnl = (price - entry_price) / entry_price

    log(f"SELL {coins} of {current_market} @ {price} "
        f"({pnl:.3%} on trade, reason: {reason})")

    log_event("SELL", current_market, price, coins, f"Exit position: {reason}")

    if pnl < 0:
        losing_streak += 1
        log(f"Losing streak increased to {losing_streak}.")
    else:
        if losing_streak > 0:
            log("Winning trade – resetting losing streak.")
        losing_streak = 0

    current_market = None
    entry_price = None


# ===========================
# Main loop
# ===========================

def main_loop() -> None:
    global trading_paused_for_today

    log("=" * 60)
    log("CRYPTO PAPER-TRADING BOT (RANDOM MULTI-MARKET SCAN)")
    log("NO REAL MONEY. NO REAL ORDERS. PUBLIC DATA ONLY.")
    log(f"Starting balance: ${START_BALANCE_USD}")
    log("=" * 60)

    ensure_trade_log_exists()

    last_price_for_equity = Decimal("0")

    while True:
        try:
            # Update daily state & drawdown using last known price
            update_daily_state(last_price_for_equity)

            # If we are in a position, manage TP/SL first
            if current_market and entry_price:
                price = get_latest_price(current_market)
                if price is None:
                    log(f"Could not get latest price for {current_market}, holding...")
                else:
                    last_price_for_equity = price
                    equity = current_equity(price)
                    pnl = (price - entry_price) / entry_price

                    log(f"Holding {current_market}: price={price}, "
                        f"P&L on trade={pnl:.3%}, equity=${equity:.2f}")

                    # Take profit?
                    if pnl >= TAKE_PROFIT_PCT:
                        exit_position("TAKE_PROFIT", price)

                    # Stop loss?
                    elif pnl <= -STOP_LOSS_PCT:
                        exit_position("STOP_LOSS", price)

                    else:
                        log_event("HOLD", current_market, price,
                                  coin_balance, "Holding open position")

            else:
                # We are flat – decide if we are allowed to trade
                price_for_equity = last_price_for_equity or Decimal("0")
                update_daily_state(price_for_equity)

                if trading_paused_for_today:
                    log("Trading paused for today due to daily drawdown. Waiting...")
                elif losing_streak >= MAX_LOSING_STREAK:
                    log(f"Losing streak {losing_streak} >= {MAX_LOSING_STREAK}; "
                        "skipping new entries for now.")
                else:
                    # Look for best new market to enter
                    score, market, price, closes = choose_best_market()
                    if market is None or price is None or score < 0:
                        log("No suitable market found this cycle.")
                    else:
                        last_price_for_equity = price
                        log(f"Best market this cycle: {market} (score {score:.4f}, price {price})")
                        enter_position(market, price)

            # Sleep until next cycle
            log(f"Sleeping for {SLEEP_SECONDS} seconds...\n")
            time.sleep(SLEEP_SECONDS)

        except KeyboardInterrupt:
            log("KeyboardInterrupt received; stopping bot.")
            break
        except Exception as e:
            log(f"Unexpected error in main loop: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main_loop()
