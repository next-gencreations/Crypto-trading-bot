# Crypto Trading Bot

Automated crypto paper-trading bot that scans multiple markets, executes algorithmic buy/sell trades, logs performance, and simulates compounding profit with a starting balance of $100.

## Features

- Paper trading (no real money)
- Multi-coin market scanning
- Automatic coin selection
- Algorithmic buy/sell logic
- Configurable profit targets
- Trade logging to CSV
- Daily performance summary
- Risk management controls
- Modular architecture (easy to extend)

## Planned Enhancements

- Machine-learning signal prediction
- Stop-loss and trailing-stop logic
- Multi-coin portfolio balancing
- Real trading via Coinbase API
- Web dashboard showing performance
- Cloud deployment support (Render, Railway, AWS)

## Requirements (for local dev)

- Python 3.10+
- `requests`
- `pandas` (optional)
- `python-dotenv`

Install requirements:

```bash
pip install -r requirements.txt
