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

### 1.1 適用スコープ

- リスク・セクター・個別銘柄集中は **household 合算**で測る。
- 発注可否・現金・NISA 枠・税務取得費は
  `owner + broker + account + instrument/currency` の権威単位で測る。
- 生活防衛資金と5年以内に使う予定資金は投資可能資産から除外する。
  金額は家計情報の権威データで明示し、不明な間は「余剰現金」と推測して
  leverage 増加や強制的な現金投入の根拠にしない。

## 2. ベンチマーク

```
主ベンチマーク:
  60% MSCI ACWI (JPY建て)             ← yfinance proxy: VT × USDJPY
  40% グローバル投資適格債 (JPY建て)   ← yfinance proxy: AGG × USDJPY
  constant weight, 月次リバランス

参考ベンチマーク:
  100% MSCI ACWI (JPY建て)             ← VT × USDJPY
```

主ベンチマークはリスクを抑えた資産形成目標、参考ベンチマークは
「株式市場を保有しただけ」と比べて銘柄選択・タイミング判断に価値があったかを見る。
評価開始後に都合よくベンチマークを切り替えない。

env で上書き可:
- `BENCHMARK_EQUITY_TICKER` (default `VT`)
- `BENCHMARK_BOND_TICKER`   (default `AGG`)
- `BENCHMARK_EQUITY_WEIGHT` (default `0.60`)
- `BENCHMARK_BOND_WEIGHT`   (default `0.40`)

## 3. ハード制約 (ex-ante、Policy Engine が gating)

| 制約 | 閾値 | env override | 違反時の挙動 |
|---|---|---|---|
| portfolio ledger integrity | `ok is not False` | — | 不整合が明示された場合、実行可能な提案を reject |
| ex-ante VaR_1d_95% | 弱気・ストレス時 `< 1.2%` / 通常時 `< 1.6%` / 強気かつ VIX<25 時 `< 2.0%` | `POLICY_VAR_THRESHOLD`（絶対上限は `POLICY_VAR_MAX_THRESHOLD`、既定2.3%） | 閾値以上なら新規 buy/add/dca/margin_buy を reject |
| current drawdown | ≤ -8% で block | `POLICY_DD_BLOCK_THRESHOLD` | 新規 buy を原則停止。実損益ガードが許可したDCAラダーだけは別上限で扱う |
| current drawdown | ≤ -5% で警戒 | `POLICY_DD_CAUTION_THRESHOLD` | urgency 降格 + size 半減 |
| current drawdown | ≤ -15% | — | 自動損切りせず、追加リスクを止めて人間の緊急レビュー |
| VIX (extreme) | < 40 | `POLICY_VIX_BLOCK_THRESHOLD` | margin_buy / short を reject、buy urgency 降格 |
| leverage_status | ∈ {warning, deleverage, emergency} | — | margin_buy reject |
| earnings 5 営業日以内 | — | — | 該当 ticker への buy を原則 reject。イベント取引の明示理由がある場合のみ後段capつきで暫定許可 |
| CVaR tail sample | 安定していること | — | margin_buy を reject。クリーン履歴不足だけが理由なら、buy系をsize半減 + urgency降格 |
| data_freshness | ≥ 0.7 | `POLICY_FRESHNESS_THRESHOLD` | high urgency を medium に降格 |

すべて `policy_engine.RULES` に実装。後付けで足すルールも本ファイルに追記すること。

VaR の3段階は `policy_engine.build_context_from_synthesis_inputs()` が、シナリオ・
レジーム・VIX・実損益ガードの状態から決める。`POLICY_VAR_THRESHOLD` を明示した場合は
その値を使うが、`POLICY_VAR_MAX_THRESHOLD` を超える値にはならない。

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
同一 ticker + direction + horizon の評価可能履歴 < 20 件
  → kelly_sizing は entry_allowed=False を返す (default-deny)
履歴あり → half-Kelly + tier 別 cap (long 5% / medium 3% / swing 2%)
Policy Engine の policy_size_adj が付いた場合は更にそれを掛ける
```

定義:
- `kelly_sizing.FALLBACK_ENTRY_ALLOWED = False` (P1-20)
- `kelly_sizing.FALLBACK_SIZE_PCT      = 0.005` (例外許可時の観察用 size)

### 6.1 集中上限

household の投資ポートフォリオ評価額（投資口座の現金を含む）に対する同一 instrument の監視上限は
long 10% / medium 5% / swing 2%、持株会銘柄は別枠10%とする。
同一 instrument に複数 tier が混在する場合は review とし、持株会以外は最も厳しい tier 上限で観測する。
これは Kelly の「新規追加上限」とは別で、既存超過を一括売却する指示ではない。
超過時は新規追加を止め、税・売買単位・open order を反映した決定論的 exit sizing を
shadow で比較してから段階是正する。

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
- `currency_policy.py` が検証するが、現行モードは `shadow`。
  valid な候補も観測用に記録するだけで、rebalance は static 目標を使う。
- **適用母数は long tier 限定**。AI が見る whole_portfolio 比率を long 母数へ誤適用しないため、
  rebalance に効くのは `basis="long_tier"` の方針のみ (data_gatherer は whole/long 両比率を AI に渡す)。
- **fail-closed**: 壊れ/期限切れ/自信不足 (confidence < 60) / 合計 ≠ 100% / basis 不一致は不採用とし、
  現行 static `CURRENCY_TARGETS` (USD 60-70% / JPY 30-40%) に戻る (機能停止ではない)。
- AI 申告の `valid_until` / `horizon_days` は無条件採用せず最大 30 日にクランプ、目標変化は ±10pt にクランプ。
- セクター / geo / NISA 売却保護は今回の動的化の対象外 (従来通り)。
- 持株会銘柄の売却判断は当面 HOLD。通貨目標の下振れだけで trim を誘発しないこと。

実装: `currency_policy.py` / `analyst.data_gatherer` (whole/long 比率注入) /
`analyst.__init__` (synthesis 後の shadow 観測)。

## 8. 機能・商品の有効化契約

「ON」は計算・観測と、実アクションへの強制適用を分けて記録する。

| 対象 | 現行モード | 実アクションへの影響 |
|---|---|---|
| 現物株・非レバレッジETF・投信 | active | Policy / readiness を通った提案を人間が最終発注 |
| 信用買い・空売り | conditional active | VaR・VIX・維持率・銘柄別上限を満たす場合だけ提案可。自動発注なし |
| オプション | signal only | IV・skew等を分析入力に使う。オプション注文機能はない |
| Kelly | shadow | 反実仮想を毎分析で記録し、実サイズは変更しない |
| FX hedge / 先物 | shadow | 経済的エクスポージャーと仮想目標を記録。注文機能はない |
| 税務取得費 | compare | legacy と総平均法を同一snapshotで比較。税務actionは新計算の検証完了まで作らない |
| GINN | candidate validation | manifest を満たす昇格済みモデルだけ推論可。未昇格時はGARCH |
| Execution plan gate | observe | would-filterを記録するが、それだけでactionを削除しない |

公開リポジトリの初期状態もこの安全境界を使う。利用者が秘密鍵や口座データを設定しても、
自動発注へは移行しない。enforce/advisory への昇格は機能ごとに別承認とする。

## 9. 改訂履歴

| 日付 | 変更 |
|---|---|
| 2026-05-16 | 初版作成 (Opus plan P-1) |
| 2026-07-01 | §7.1 追加: ベンチマーク固定と外貨比率の AI 動的判断を分離 (自動発注なし・long母数限定・fail-closed) |
| 2026-07-28 | household/口座スコープ、二重ベンチマーク、相場別VaR、Kelly 20件、集中上限、shadow-first 有効化契約を確定 |
