# Zerodha Swing Trading Agent

A local and deployable NSE swing-trading research dashboard. It is paper-only: the app has no broker order-placement methods. The optional Kite adapter is a separate read-only scaffold and is not exposed by the web app.

## Run locally

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Open `http://127.0.0.1:5000`. Run the checks with:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Deploy to Vercel

The app uses Flask as a Python Function. Vercel Functions have temporary local filesystems, so configure MongoDB Atlas before deployment.

1. Push this project to a private GitHub repository.
2. In Vercel, import the repository and use the included `vercel.json` and `requirements.txt`.
3. Configure the Atlas cluster, database user, and network access for the deployment.
4. Add these Vercel environment variables for **Production, Preview, and Development** as appropriate:
   - `MONGODB_URI`: the Atlas SRV connection URI for the `paper_trading` database user.
   - `MONGODB_DATABASE`: `paper_trading`.
   - `APP_ACCESS_TOKEN`: a long, random value. All routes require it when deployed; send it as `X-App-Token` for API calls. The simple HTML page does not provide a login form, so access it with `/?token=...` only if necessary; browser URL tokens can appear in history/logs. Prefer the header/API or place the app behind Vercel protection.
5. Redeploy after setting environment variables.

The health endpoint is `/health`; it is also protected by `APP_ACCESS_TOKEN`. Vercel’s project-level deployment protection can add an additional access layer. Do not put secrets in Git or frontend code. `MONGODB_URI` and `APP_ACCESS_TOKEN` are runtime-only secrets.

Vercel is suitable for the interactive dashboard and request/response API. It does not run a permanent background scanner or continuous exit monitor. No automated trading loop is deployed.

## Data format and scanner

No stock universe or market data is bundled. The scanner reads local CSV files from `data\market\SYMBOL.csv` (configurable in `config.yaml`) with headers:

`date,open,high,low,close,volume,completed,received_at`

Dates use ISO `YYYY-MM-DD`; `completed` must identify finalized daily candles; `received_at` must include a timezone. On Vercel, files committed under `data/market` are packaged with the function, but this is a static snapshot and not a reliable market-data feed. Use only data you are authorized to store and deploy. Missing/stale/malformed data yields `NO_TRADE`.

## Paper trading

The dashboard displays cash, daily P/L, positions, and trade history. Scanning never opens a trade. Paper entries can be made through the API only after strategy, quantity, capital, position-count, and daily-loss checks pass; manual exit actions remain simulated. SQLite is used locally when no database URI is set; MongoDB Atlas is selected when `MONGODB_URI` is set. PostgreSQL remains supported via `DATABASE_URL`.

Paper fills apply configured slippage against the trader. When a candle touches stop and target, the stop is assumed to execute first; a gap below the stop fills from the candle open, then slippage. Stop-losses do not guarantee maximum loss. Transaction-cost settings in `config.yaml` default to zero and are estimates, not a Zerodha contract-note calculator.

## API

All routes require `X-App-Token: <APP_ACCESS_TOKEN>` when deployed with `VERCEL` set.

- `GET /health`
- `GET /api/summary`
- `POST /api/scan` with `{"symbols":["RELIANCE","INFY"]}`
- `POST /api/paper-trades` with `symbol`, `entry_price`, `stop_loss`, `target_price`, `signal_key`, and optional `quantity` no larger than the computed risk quantity
- `POST /api/paper-trades/<id>/close` with `reference_price` and optional `reason`

API responses identify simulated data. No endpoint places, modifies, or cancels a real broker order.

## Historical research

Run a local CSV backtest with:

```powershell
.\.venv\Scripts\python.exe backtest_cli.py RELIANCE
```

It enters no earlier than the next candle and applies configured fill/cost assumptions. Results depend on data quality, corporate actions, survivorship, and simplified execution. Historical performance does not predict future returns. No licensed production dataset has been validated here.

## Zerodha read-only scaffold

`broker\zerodha_client.py` uses the official Kite login URL, request-token checksum exchange, and read-only profile/positions/holdings/margins endpoints. It has no order-write methods and is not connected to an account. Credentials must be supplied through server-side environment variables and never shared in chat. Kite access tokens expire by the next day.

## Live execution status and current references

Live order execution is not implemented. Before considering it, recheck Zerodha pricing/terms, order-placement controls, SEBI and exchange requirements, broker onboarding, static-IP mapping, hosting, registration, audit/logging and daily logout rules. The following references were reviewed on 26 October 2026 and must be rechecked before any live work:

- [Kite Connect v3 authentication](https://kite.trade/docs/connect/v3/user/)
- [Kite Connect v3 API documentation](https://kite.trade/docs/connect/v3/)
- [Zerodha Kite Connect pricing announcement](https://kite.trade/forum/discussion/15015/revising-kite-connect-fees-from-2000-to-500-per-month)
- [SEBI retail algorithmic trading circular](https://www.sebi.gov.in/sebi_data/attachdocs/feb-2025/1738676110219.pdf)
- [NSE implementation standards](https://nsearchives.nseindia.com/content/circulars/INVG67858.pdf)
- [NSE retail-algo FAQ](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/FAQ_Retail%20Algo_03112025_NSE.pdf)

This is not a compliance determination. Get current confirmation from Zerodha and qualified compliance advice before any live-trading development.
