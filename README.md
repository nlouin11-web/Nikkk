# BTC 15-min Paper Bot (Kalshi KXBTC15M)

Paper trading only: no account, no API key, no real orders.
Runs 4 strategies against every live 15-minute Bitcoin window and tracks P/L after Kalshi fees.

Deploy on Railway: New Project -> Deploy from GitHub repo -> this repo, branch `btc15-bot`.
Add a Volume mounted at `/data`, then Generate Domain to open the dashboard.

Run locally: `python main.py` (dashboard on http://localhost:8080). Tests: `python -m unittest test_main`.
