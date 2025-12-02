from decimal import Decimal, getcontext
from datetime import datetime, timezone, date
import time
import csv
import requests
import os

# Higher precision for crypto maths
getcontext().prec = 28

# === CONFIG ===
START_BALANCE_USD = Decimal("100")
MARKETS = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "ADA-USD"]

SLEEP_SECONDS = 6 * 60          # 6 minutes
CANDLE_GRANULARITY = 300        # 5-minute candles
LOOKBACK_CANDLES = 100

TAKE_PROFIT_PCT = Decimal("0.010")         # +1.0%
STOP_LOSS_PCT   = Decimal("0.015")         # -1.5%
POSITION_SIZE_FRACTION = Decimal("0.3")    # 30% of USD per trade

MIN_TREND_STRENGTH = Decimal("0.002")      # short MA must be > long MA by 0.2%
RSI_BUY_MIN = Decimal("40")
RSI_BUY_MAX = Decimal("65")

MIN_VOLATILITY = Decimal("0.002")          # 0.2% avg move per candle
MAX_VOLATILITY = Decimal("0.03")           # 3% avg move per candle

MAX_DAILY_DRAWDOWN = Decimal("0.05")       # 5% daily drawdown limit
MAX_LOSING_STREAK = 3                      # after 3 losses, pause

PUBLIC_API_BASE = "https://api.exchange.coinbase.com"
TRADE_LOG = "live_sim_trade_history.csv"

# === STATE ===
usd_balance = START_BALANCE_USD
coin_balance = Decimal("0")
current_market = None
entry_price = None
trade_count = 0
losing_streak = 0

equity_peak_today = START_BALANCE_USD
today = date.today()
trading_paused_for_today = False  # triggered by daily drawdown

# create trade log if missing
if not os.path.exists(TRADE_LOG):
    with open(TRADE_LOG, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "Timestamp", "Market", "Action", "Price",
            "Amount", "USD_Balance", "Coin_Balance",
            "Portfolio_Value", "Profit_Loss"
        ])


def log(msg: str):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} | {msg}")


# --- DATA FETCHING ---

def fetch_ticker(market: str) -> Decimal:
    url = f"{PUBLIC_API_BASE}/products/{market}/ticker"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    return Decimal(data["price"])


def fetch_candles(market: str, limit=LOOKBACK_CANDLES):
    end_time = int(time.time())
    start_time = end_time - limit * CANDLE_GRANULARITY

    params = {
        "start": datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat(),
        "end": datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat(),
        "granularity": CANDLE_GRANULARITY,
    }
    url = f"{PUBLIC_API_BASE}/products/{market}/candles"
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    candles = r.json()
    closes = [Decimal(str(c[4])) for c in candles]
    closes.reverse()  # chronological
    return closes


# --- INDICATORS ---

def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / Decimal(period)


def rsi(values, period=14):
    if len(values) <= period:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = values[-i] - values[-i - 1]
        if diff > 0:
            gains.append(diff)
        elif diff < 0:
            losses.append(-diff)

    if not gains and not losses:
        return Decimal("50")

    avg_gain = sum(gains) / Decimal(period) if gains else Decimal("0")
    avg_loss = sum(losses) / Decimal(period) if losses else Decimal("0")

    if avg_loss == 0:
        return Decimal("100")

    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def avg_volatility(values, period=20):
    """Average absolute % change per candle."""
    if len(values) <= period:
        return None
    moves = []
    for i in range(1, period + 1):
        prev = values[-i - 1]
        curr = values[-i]
        if prev == 0:
            continue
        moves.append(abs((curr - prev) / prev))
    if not moves:
        return None
    return sum(moves) / Decimal(len(moves))


# --- SCORING MARKETS ---

def score_market(market: str):
    """
    Return (score, current_price, closes) for this market.
    Higher score = more attractive. Negative score = skip.
    """
    try:
        closes = fetch_candles(market)
        if len(closes) < 30:
            return (Decimal("-1"), None, closes)

        price = closes[-1]
        ma_short = sma(closes, 9)
        ma_long = sma(closes, 21)
        current_rsi = rsi(closes, 14)
        vol = avg_volatility(closes, 20)

        if not ma_short or not ma_long or not current_rsi or not vol:
            return (Decimal("-1"), price, closes)

        # trend strength
        trend = (ma_short - ma_long) / ma_long

        # filters: only uptrend, not overbought, reasonable volatility
        if trend < MIN_TREND_STRENGTH:
            return (Decimal("-1"), price, closes)
        if current_rsi < RSI_BUY_MIN or current_rsi > RSI_BUY_MAX:
            return (Decimal("-1"), price, closes)
        if vol < MIN_VOLATILITY or vol > MAX_VOLATILITY:
            return (Decimal("-1"), price, closes)

        # score: stronger trend better, mid-range RSI best
        rsi_centered = Decimal("55") - abs(current_rsi - Decimal("55"))
        score = trend * Decimal("100") + rsi_centered / Decimal("100")
        return (score, price, closes)
    except Exception as e:
        log(f"{market}: error scoring market: {e}")
        return (Decimal("-1"), None, [])


def choose_best_market():
    best = (Decimal("-999"), None, None, None)  # score, market, price, closes
    for m in MARKETS:
        score, price, closes = score_market(m)
        if price is None:
            continue
        log(f"Market {m} score {score:.4f}")
        if score > best[0]:
            best = (score, m, price, closes)
    return best  # (score, market, price, closes)


# --- RISK & LOGGING ---

def portfolio_value(last_price: Decimal | None):
    if current_market and last_price:
        return usd_balance + coin_balance * last_price
    return usd_balance


def write_trade(timestamp, market, action, price, amount, pv, pl):
    with open(TRADE_LOG, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            timestamp, market, action, str(price),
            str(amount), str(usd_balance), str(coin_balance),
            str(pv), str(pl)
        ])


def update_daily_risk(pv: Decimal):
    global equity_peak_today, today, trading_paused_for_today

    now = datetime.now(timezone.utc).date()
    if now != today:
        # new day: reset
        today = now
        equity_peak_today = pv
        trading_paused_for_today = False
        log("New day – resetting daily risk counters.")

    if pv > equity_peak_today:
        equity_peak_today = pv

    drawdown = (equity_peak_today - pv) / equity_peak_today
    if drawdown >= MAX_DAILY_DRAWDOWN:
        trading_paused_for_today = True
        log(f"Daily drawdown hit ({drawdown*100:.2f}%). "
            f"Pausing new trades for today.")


# --- SIMULATED ORDERS ---

def simulate_buy(market: str, price: Decimal):
    global usd_balance, coin_balance, current_market, entry_price, trade_count

    usd_to_spend = usd_balance * POSITION_SIZE_FRACTION
    if usd_to_spend < Decimal("10"):
        log("Not enough USD to buy.")
        return

    amount = (usd_to_spend / price).quantize(Decimal("0.00000001"))
    usd_balance -= usd_to_spend
    coin_balance += amount
    current_market = market
    entry_price = price
    trade_count += 1

    pv = portfolio_value(price)
    pl = pv - START_BALANCE_USD
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    log(f"BUY {amount} {market} @ {price} | PV ≈ ${pv:.2f}")
    write_trade(ts, market, "BUY", price, amount, pv, pl)
    update_daily_risk(pv)


def simulate_sell(price: Decimal):
    global usd_balance, coin_balance, current_market, entry_price, trade_count, losing_streak

    if coin_balance <= Decimal("0"):
        return

    amount = coin_balance
    usd_before = usd_balance
    usd_gained = (amount * price).quantize(Decimal("0.01"))

    coin_balance = Decimal("0")
    usd_balance += usd_gained
    trade_count += 1

    pv = portfolio_value(price)
    pl = pv - START_BALANCE_USD
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    log(f"SELL {amount} {current_market} @ {price} | PV ≈ ${pv:.2f} | P/L ${pl:+.2f}")
    write_trade(ts, current_market, "SELL", price, amount, pv, pl)
    update_daily_risk(pv)

    # update losing streak
    if usd_balance < usd_before:
        losing_streak += 1
        log(f"Losing streak increased to {losing_streak}")
    else:
        losing_streak = 0

    # reset position markers
    current_market = None
    entry_price = None


# --- MAIN LOOP ---

def loop():
    global entry_price, current_market, losing_streak

    log("=" * 60)
    log(f"Starting safer simulated bot with ${START_BALANCE_USD} USD")

    while True:
        try:
            last_price_for_pv = None
            if current_market:
                last_price_for_pv = fetch_ticker(current_market)
            pv = portfolio_value(last_price_for_pv)
            update_daily_risk(pv)

            if trading_paused_for_today:
                log("Trading paused for today due to drawdown. Watching only.")
            elif losing_streak >= MAX_LOSING_STREAK:
                log(f"Losing streak {losing_streak} reached. "
                    "Skipping new entries this cycle.")

            if current_market is None:
                # Only open new trades if not paused AND losing streak ok
                if (not trading_paused_for_today) and (losing_streak < MAX_LOSING_STREAK):
                    log("Scanning markets for new opportunity...")
                    score, market, price, closes = choose_best_market()
                    if market and score > 0:
                        log(f"Chosen market {market} score={score:.4f} price={price}")
                        simulate_buy(market, price)
                    else:
                        log("No attractive market found; staying in cash.")
                else:
                    log("Conditions not met for new trade; staying in cash.")
            else:
                # We have a position: manage it
                price = fetch_ticker(current_market)
                if not entry_price:
                    entry_price = price

                change_pct = (price - entry_price) / entry_price
                pv = portfolio_value(price)
                pl = pv - START_BALANCE_USD

                log(f"Holding {current_market} | Price {price} | "
                    f"Change {change_pct*100:.2f}% | PV ${pv:.2f}")

                if change_pct >= TAKE_PROFIT_PCT:
                    log("Take profit hit; selling.")
                    simulate_sell(price)
                elif change_pct <= -STOP_LOSS_PCT:
                    log("Stop loss hit; selling.")
                    simulate_sell(price)

        except Exception as e:
            log(f"Error in main loop: {e}")

        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    loop()
