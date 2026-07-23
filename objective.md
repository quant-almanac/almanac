# ALMANAC Objective — 目的関数定義書

> Codex 3 ラウンド目 + Opus plan P-1 で確定。すべての policy rule・sizing・health check はここから派生する。  
> 本書を変えない限り Policy Engine の閾値も AI プロンプトの行動方針も再定義しない。

## 1. 最大化対象

**ALMANAC が最大化するもの** =
```
税引後・手数料後・JPY建て実質純資産 (年率 TWR; Time-Weighted Return, Modified Dietz 計算)
```

- 「税引後」: 国内分離課税 20.315%、米国配当源泉税 10% (NISA 適用部分は控除) を控除済みベース
- 「手数料後」: 売買手数料、為替手数料、信託報酬を全て控除
- 「JPY建て」: USD 建てポジションも日次 close USDJPY で円換算
- 「TWR」: 入出金 (cash_flow event) の影響を controlled out した投資判断品質指標

## 2. ベンチマーク

```
60% MSCI ACWI (JPY建て)   ← yfinance proxy: VT × USDJPY
40% グローバル投資適格債 (JPY建て)   ← yfinance proxy: AGG × USDJPY
constant weight, 月次リバランス (P2 で実装)
```

env で上書き可:
- `BENCHMARK_EQUITY_TICKER` (default `VT`)
- `BENCHMARK_BOND_TICKER`   (default `AGG`)
- `BENCHMARK_EQUITY_WEIGHT` (default `0.60`)
- `BENCHMARK_BOND_WEIGHT`   (default `0.40`)

## 3. ハード制約 (ex-ante、Policy Engine が gating)

| 制約 | 閾値 | env override | 違反時の挙動 |
|---|---|---|---|
| ex-ante VaR_1d_95% | ≤ 1.2% | `POLICY_VAR_THRESHOLD` | 新規 buy/add/dca/margin_buy を reject |
| current drawdown | > -8% で reject | `POLICY_DD_BLOCK_THRESHOLD` | 新規 buy を全停止 |
| current drawdown | > -5% で警戒 | `POLICY_DD_CAUTION_THRESHOLD` | urgency 降格 + size 半減 |
| VIX (extreme) | < 40 | `POLICY_VIX_BLOCK_THRESHOLD` | margin_buy / short を reject、buy urgency 降格 |
| leverage_status | ∈ {warning, deleverage, emergency} | — | margin_buy reject |
| earnings 5 営業日以内 | — | — | 該当 ticker への buy reject |
| data_freshness | ≥ 0.7 | `POLICY_FRESHNESS_THRESHOLD` | high urgency を medium に降格 |

すべて `policy_engine.RULES` に実装。後付けで足すルールも本ファイルに追記すること。

## 4. 受入れ基準 (継続評価)

```
12 ヶ月 rolling で 「税引後・手数料後 portfolio TWR ≥ benchmark TWR + 200bps」
かつ
12 ヶ月 rolling で 「最大 DD ≤ 15%」
```

両方を継続して満たした時点で初めて **「資産最大化 OS」** と名乗れる。
それまでは「Policy Engine を備えた意思決定支援システム」と表記する。

### 4.1 測定データの信頼起点 (CLEAN_NAV_SINCE)

NAV 系列はバグ修正前 (cost_jpy /10000 誤適用・FX 150 固定・通貨 USD 固定 等、〜2026-04-17 の
P0/P1 audit, 〜2026-05-25 stabilization) の期間が汚染されている。TWR/excess α/VaR/CVaR/DD/stance
はこの汚染期間を**測定・意思決定から除外**する。実装は `config_clean_baseline.py`。

env で上書き可:
- `ALMANAC_CLEAN_NAV_SINCE` (default `2026-05-25`) — 信頼できる NAV の起点日
- `ALMANAC_MIN_CLEAN_DAYS`  (default `20`) — TWR/CVaR を確定値扱いする最小クリーン営業日数

原則:
- クリーン履歴 < `MIN_CLEAN_DAYS` の間は TWR/excess α を数値で出さず「データ不足」と縮退し、
  **stance override / alpha hurdle の根拠に使わない**。
- excess α 再解禁には「対象期間の cash_flow 台帳が健全」も条件 (積立の controlled-out が前提)。

## 5. no-trade の許容

```
priority_actions = []  は valid な出力。
件数ノルマは設けない。
期待 alpha が手数料・税後で 50bps を下回る候補は採用しない (alpha hurdle)。
```

これは下記との明示的整合性を保つために定めた:
- `analyst/__init__.py` プロンプト (2351 行ほか): 件数ノルマ廃止済み (P0-4)
- `daily_health_check.py:71`: 「actions < 3 件 = 異常」廃止済み (P0-9)
- `policy_engine.py`: rejected はそのまま受け入れ、accept させない

## 6. Sizing の原則

```
履歴 < 5 件 (MIN_TRADES_FOR_KELLY) → kelly_sizing は entry_allowed=False を返す (default-deny)
履歴あり → half-Kelly + tier 別 cap (long 5% / medium 3% / swing 2%)
Policy Engine の policy_size_adj が付いた場合は更にそれを掛ける
```

定義:
- `kelly_sizing.FALLBACK_ENTRY_ALLOWED = False` (P1-20)
- `kelly_sizing.FALLBACK_SIZE_PCT      = 0.005` (例外許可時の観察用 size)

## 7. AI と Policy の役割分担

```
AI (Sonnet × 3 + Opus):   候補生成器・情報統合
Policy Engine:            deterministic な制約フィルタ
人間:                     最終発注、tunable_params 承認
```

順序は **AI → Policy Engine → 人間** で、逆ではない。
AI の判定を quant が後付けで正当化する流れ (旧 BL の confidence laundering) は禁止。
BL の View 入力源は P2 で independent alpha (factor signal / analyst consensus) に置換。

## 7.1 ベンチマーク固定 と 外貨比率の動的判断 (2026-07)

ベンチマーク (§2) と外貨配分目標は **別物** であり、混同しない。

```
ベンチマーク (60% VT / 40% AGG):   成績評価の固定された物差し。配分指示ではない。今回変更しない。
外貨比率目標 (USD/JPY):            市況に応じて AI が判断する動的方針。
自動発注:                          しない。Policy Engine と人間の最終実行は不変。
```

- AI は `currency_target_recommendation` で外貨比率を判断する (basis / usd_target_pct /
  jpy_target_pct / confidence_pct / horizon_days / valid_until / reason / review_triggers)。
- `currency_policy.py` が検証し、valid なら `currency_policy_state.json` に保存
  (履歴は append-only `currency_policy_log.jsonl`)。次回 rebalance の通貨目標に採用される。
- **適用母数は long tier 限定**。AI が見る whole_portfolio 比率を long 母数へ誤適用しないため、
  rebalance に効くのは `basis="long_tier"` の方針のみ (data_gatherer は whole/long 両比率を AI に渡す)。
- **fail-closed**: 壊れ/期限切れ/自信不足 (confidence < 60) / 合計 ≠ 100% / basis 不一致は不採用とし、
  現行 static `CURRENCY_TARGETS` (USD 60-70% / JPY 30-40%) に戻る (機能停止ではない)。
- AI 申告の `valid_until` / `horizon_days` は無条件採用せず最大 30 日にクランプ、目標変化は ±10pt にクランプ。
- セクター / geo / NISA 売却保護は今回の動的化の対象外 (従来通り)。
- 持株会 (9999.T) 売却判断は当面 HOLD。通貨目標の下振れが 9999.T trim を誘発しないこと。

実装: `currency_policy.py` / `rebalance_engine.calculate_rebalance_actions(currency_targets=...)` /
`analyst.data_gatherer` (whole/long 比率注入) / `analyst.__init__` (synthesis 後 ingest)。

## 8. 改訂履歴

| 日付 | 変更 |
|---|---|
| 2026-05-16 | 初版作成 (Opus plan P-1) |
| 2026-07-01 | §7.1 追加: ベンチマーク固定と外貨比率の AI 動的判断を分離 (自動発注なし・long母数限定・fail-closed) |
