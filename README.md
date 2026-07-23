# ALMANAC

*[日本語](README.ja.md)*

**ALMANAC** is a personal, AI-assisted portfolio management and risk-control system. It pairs a quantitative Python backend with a Next.js dashboard to run daily portfolio analysis, market screening, and disciplined risk management for a real long-term investment account — with hard, deterministic guardrails sitting between any AI suggestion and an actual trade.

**This is not an automated trading bot.** There is no broker order API anywhere in this codebase. The AI proposes, the policy engine either blocks or allows the proposal through, and a human places the actual order at their broker.

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
| **Observability** | NAV/TWR performance tracking against benchmark (a Modified Dietz cash-flow-adjusted approximation, not a daily sub-period-exact TWR), with a verification page that reports actual measured performance rather than a fixed claim |

## Architecture

- **Backend** — Python 3.12 / FastAPI. Portfolio optimization ([PyPortfolioOpt](https://github.com/robertmartin8/PyPortfolioOpt), [riskfolio-lib](https://riskfolio-lib.readthedocs.io/), [skfolio](https://skfolio.org/)), GARCH risk modeling ([arch](https://arch.readthedocs.io/)), FinBERT sentiment (`transformers` / `torch`), Claude (Anthropic) and DeepSeek for LLM-assisted analysis.
- **Frontend** — Next.js 16 (App Router) / React 19 / TypeScript. A single console covering portfolio, screening, risk, scenarios, strategy, margin, NISA, AI decision support, execution log, and a performance-verification page.
- **Privacy layer** — ALMANAC runs locally, but some of its configured AI features do send portfolio context (holdings, quantities, P&L, allocation) to an external LLM. Non-Anthropic calls that are meant to carry only public or anonymized data (disclosure-feature extraction, the analyst debate, Red Team, screening) go through an allowlist gate (`almanac/llm_safety.py`). Separately, "book-aware" call sites (the chat assistant, case-based decision support, some guardrail alerts) send portfolio context to Anthropic — and in one path, to DeepSeek — by design, and log that usage. See [Public Repository Safety](#public-repository-safety) for exactly what never leaves the machine regardless.

## Configuration

Copy `.env.example` to `.env` and fill in what you need. Nothing is required just to read the code — these only matter if you actually run the system.

**Required for AI features**

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude — powers AI decision support, case analysis, and LLM-generated portfolio views |
| `DEEPSEEK_API_KEY` | DeepSeek — cost-efficient screening and long-term-scan harness |

**Optional**

| Variable | Purpose |
|---|---|
| `FRED_API_KEY` | Macro data (Federal Reserve Economic Data) for regime/risk context |
| `FINNHUB_API_KEY` | Supplementary market data |
| `GEMINI_API_KEY`, `GOOGLE_AI_API_KEY` | Alternative LLM backend |
| `GROQ_API_KEY` | Alternative fast-inference LLM backend |
| `OPENROUTER_API_KEY` | LLM routing/aggregator, alternative backend |
| `TELEGRAM_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Push notifications for alerts and daily briefings |
| `ALMANAC_API_KEY`, `NEXT_PUBLIC_ALMANAC_API_KEY` | Auth key for write endpoints (recording trades, editing tuning params) — read-only browsing works without it |
| `ALMANAC_ESPP_*` | Employee-stock-plan tracking; disabled (`0`) by default |
| `ALMANAC_CONTRIBUTION_SCHEDULE_JSON` | Recurring cash-flow definitions; empty by default |
| `ALMANAC_CLEAN_NAV_SINCE`, `ALMANAC_MIN_CLEAN_DAYS` | Performance-measurement window hygiene |
| `ALMANAC_PRIVACY_MODE` | Gates *book-aware* external LLM calls (chat, decision support, guardrail alerts, final daily synthesis) — see below |

### Privacy mode

Some AI features intentionally send portfolio context (holdings, balances, P&L) to an external model — see [Public Repository Safety](#public-repository-safety) for exactly which ones. `ALMANAC_PRIVACY_MODE` controls whether those specific calls are allowed to run at all:

| Value | Effect |
|---|---|
| `strict_local` (default) | No book-aware call leaves the machine. Chat / decision-support / guardrail-alert / final-synthesis call sites return a local "disabled" response instead of calling out. |
| `anthropic_book_aware` | Book-aware calls to Anthropic only. |
| `multi_provider_book_aware` | Book-aware calls to any configured provider (this codebase's original, pre-gate behavior). |

Public/anonymized calls (screening, disclosure-feature extraction) are unaffected by this setting — they never carry portfolio data in the first place. Every call site with a `assert_book_aware_allowed()` gate is enumerated (and enforced by a regression test) in `tests/test_llm_call_site_gating.py`.

## Public Repository Safety

This repository intentionally does not track local portfolio state, broker exports, databases, logs, screenshots, local AI-tool sessions, or API keys.

Files such as `holdings.json`, `account.json`, `nisa_portfolio.json`, `trade_history.csv`, and `almanac.db` are ignored by Git and never leave the local machine. Worked examples use a rounded placeholder portfolio size rather than any real figure. `scripts/check_public_safety.py` scans tracked files for known private identifiers and secret-key patterns; it's meant to be run before every push.

If you're preparing your own public release from a fork of this project, rotate any token that was ever committed or pasted into local tool settings before publishing repository history.

## Getting started

### Backend

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# API keys are read from ~/.almanac_secrets (shell-style KEY=VALUE, one per
# line), not from a project-local .env file — nothing in this repo loads
# dotenv. Copy .env.example there and fill in your own values:
cp .env.example ~/.almanac_secrets
chmod 600 ~/.almanac_secrets

python scripts/init_private_state.py   # seeds local state files with small
                                        # demo values (sample cash + SPY),
                                        # not your real portfolio

./start_v5.sh                      # FastAPI on :8000 only — see below for the dashboard
```

`start_v5.sh` starts the FastAPI backend and nothing else; the script's own comments say the Next.js dashboard is expected to be managed separately (a macOS LaunchAgent, in the original setup). To run the dashboard yourself:

```bash
cd frontend
npm install
npm run dev                        # http://localhost:3000, talking to the FastAPI backend above
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

## Disclaimer

This is a personal project built around one person's own portfolio. It is not investment advice, has not been independently audited for correctness, and is shared as-is for anyone curious how the system works. Use any part of it at your own risk.

## License

[MIT](LICENSE)
