# ALMANAC

**ALMANAC** is a personal, AI-assisted portfolio management and risk-control system. It pairs a quantitative Python backend with a Next.js dashboard to run daily portfolio analysis, market screening, and disciplined risk management for a real long-term investment account — with hard, deterministic guardrails sitting between any AI suggestion and an actual trade.

This repository is a **public, sanitized snapshot** of that system. Runtime data, credentials, and anything that could identify the account owner are intentionally excluded — see [Public Repository Safety](#public-repository-safety).

## What it does

The objective function is explicit and version-controlled ([`objective.md`](objective.md)): maximize **after-tax, after-fee, JPY-denominated time-weighted return**, benchmarked against a 60% global equity / 40% global bond blend, subject to hard risk limits (VaR, drawdown, VIX-based circuit breakers) enforced by a deterministic policy engine — not by an LLM's judgment call.

| Area | What it does |
|---|---|
| **Portfolio & risk** | Black-Litterman optimization with LLM-generated views, GJR-GARCH volatility modeling, market-regime detection (bull / neutral / bear / crash), concentration and human-capital-exposure limits |
| **AI decision support** | Multi-model analysis (Claude + DeepSeek, cost-routed by task) for case-based decisions — trim, add, rebalance, tax-loss harvest — all gated by deterministic policy rules before anything reaches an order |
| **Screening & signals** | Long-term JP/US fundamental screening, disclosure-driven catalyst detection (EDINET / TDnet / EDGAR filings), margin and short-sale candidate screening, insider-cluster and IPO tracking |
| **Execution & guardrails** | Daily/monthly drawdown circuit breakers, VaR- and VIX-based trade blocking, an append-only event ledger for full auditability, open-order-aware position sizing |
| **Tax & accounts** | FIFO/LIFO/loss-harvest/gain-minimize tax-lot strategies, NISA allocation tracking, employee-stock-plan concentration management |
| **Observability** | NAV/TWR performance tracking (Modified Dietz) against benchmark, with a verification page that reports actual measured performance rather than a fixed claim |

## Architecture

The application stack is Python, FastAPI, Next.js, React, and TypeScript.

- **Backend** — Python 3.12 / FastAPI. Portfolio optimization ([PyPortfolioOpt](https://github.com/robertmartin8/PyPortfolioOpt), [riskfolio-lib](https://riskfolio-lib.readthedocs.io/), [skfolio](https://skfolio.org/)), GARCH risk modeling ([arch](https://arch.readthedocs.io/)), FinBERT sentiment (`transformers` / `torch`), Claude (Anthropic) and DeepSeek for LLM-assisted analysis.
- **Frontend** — Next.js 16 (App Router) / React 19 / TypeScript. A single console covering portfolio, screening, risk, scenarios, strategy, margin, NISA, AI decision support, execution log, and a performance-verification page.
- **Privacy layer** — every external LLM call is routed through a sanitizer (`almanac/llm_safety.py`) that strips holdings, balances, and other book data before anything leaves the machine. External models see anonymized market context, never the actual portfolio.

## Public Repository Safety

This repository intentionally does not track local portfolio state, broker exports, databases, logs, screenshots, local AI-tool sessions, or API keys.

Files such as `holdings.json`, `account.json`, `nisa_portfolio.json`, `trade_history.csv`, and `almanac.db` are ignored by Git and never leave the local machine. Worked examples in the docs use a rounded placeholder portfolio size rather than any real figure.

If you're preparing your own public release from a fork of this project, rotate any token that was ever committed or pasted into local tool settings before publishing repository history.

## Getting started

### Backend

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env               # fill in your own API keys
python scripts/init_private_state.py   # creates empty local state files from examples/

./start_v5.sh                      # FastAPI on :8000, Next.js dashboard on :3000
```

### Frontend only

```bash
cd frontend
npm install
npm run dev                        # http://localhost:3000
```

Write endpoints require `ALMANAC_API_KEY` (or a key file at `~/.config/almanac/api_key`).

## Project structure

```
almanac/                 core package — runtime config, LLM safety layer, DB migrations, observability
analyst/                 LLM-assisted analysis pipeline (multi-model, case-based)
api/                     FastAPI routes
frontend/                Next.js dashboard
examples/private_state/  templates for local-only state files (never committed)
tests/                   pytest suite
```

Most other top-level `.py` files are single-purpose modules — screeners, data fetchers, the policy and risk engines, tax tooling — rather than parts of a package. See individual file docstrings for details.

## Notes on naming

The project was previously named **KAIROS**, and earlier still, NexusTrader. The KAIROS-era compatibility code (legacy env var names, the `kairos` package import alias) has been fully removed; `ALMANAC_*` names and the `almanac` package are the only supported path going forward. A couple of NexusTrader-era fallbacks (e.g. reading an existing `nexustrader.db` if `almanac.db` isn't present yet) remain in `almanac/runtime_config.py` for anyone migrating an even older local setup.

## Disclaimer

This is a personal project built around one person's own portfolio. It is not investment advice, has not been independently audited for correctness, and is shared as-is for anyone curious how the system works. Use any part of it at your own risk.
