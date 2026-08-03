# ALMANAC システム仕様

*[English](SYSTEM_SPEC.md) · [README](../README.ja.md) · [モジュール台帳](MODULE_CATALOG.ja.md)*

この文書は、現在のソースツリーが実装している契約を記述します。README より厳密に、計算が存在するだけなのか、影実行なのか、提案を変えられるのか、人が実行可能な推薦まで到達するのかを区別します。ローカルの保有・証券会社証跡・モデルbundle・ログ・秘密情報は公開リポジトリに含みません。

## 1. 範囲・権限・状態語

ALMANAC は意思決定支援であり、注文ルーティングではありません。証券会社の認証情報を保持して注文を送るモジュールはありません。権限の流れは次です。

`計測 → AI候補 → 決定論的正規化 → policy → post-filter → readiness → 人が注文 → 人/証券会社の約定記録`

状態語の意味は固定です。

| 状態 | 契約 |
|---|---|
| live | 通常の日次経路で使用 |
| optional | 明示設定した場合だけ使用 |
| shadow | 反実仮想を計算・記録するが実actionを変更不可 |
| advisory | 推薦や数量は変更できるが発注不可 |
| review | 証拠不足で人の確認が必要 |
| blocked | 現在のactionは実行不可 |
| unwired | コードはあるが日次判断は未消費 |
| retired | 互換・監査・履歴目的だけで残存 |

公開初期値は意図的に混在します。現物・非レバレッジ投信は利用可、信用・空売りは条件付き、optionsはシグナルだけ、Kelly・集中上限・FXヘッジは影実行、GINNは昇格までdefault-deny、execution-planはobserve開始です。投資方針の権威は`objective.md`です。

## 2. 日次分析トランザクション

`portfolio_analyst.py --force` は `analyst.run_analysis()` を呼び、1回の論理的分析トランザクションとして次を行います。

1. secretsを読み、有効でないprivacy modeを拒否
2. macro event、technical、news、earnings、VIX、DCA、execution plan、scenarioを必要時更新
3. 過去推薦を検証・採点
4. portfolioとmarket contextを取得
5. risk、regime、観測専用policyを計算
6. enriched decision snapshotを構築・凍結
7. 5つの専門laneをtimeout付きで実行
8. 反証、意見不一致、任意judgeを実行
9. Claude Opusが構造化actionを統合
10. provenance、口座identity、quote、sizing contextを付与
11. policy、exit sizing、post-filter、readinessを適用
12. 実actionを変えずKelly/FX反実仮想を実行
13. 最終readinessからnarrativeを再構築
14. 分析、全stage、推薦観測、対象action stateを保存

任意laneの欠落はそのlaneだけをdegradedにします。portfolio欠落、decision snapshot凍結失敗、final tool結果のtruncate、post-filter契約失敗を成功扱いしません。

## 3. Identity と所有境界

tickerだけではpositionを特定できません。3種類のkeyを分けます。

| Key | 構成 | 用途 |
|---|---|---|
| `PositionIdentity` | owner + broker + account + canonical instrument | 保有・売却・action state・税候補 |
| `AccountResourceIdentity` | owner + broker + account + currency | 現金・買付余力 |
| `ExecutionCashWallet` | owner + broker + settlement pool + currency | 実行可能予算の予約 |
| `NisaCapacityIdentity` | owner + broker + account + NISA type + tax year | 年間/生涯枠 |

`position_identity.py` がaliasを正規化し、税候補、conflict、推薦、action ID、action state、API、governance、broker reconciliationまで伝播します。owner不明を推測しません。保有ゼロも、ローカルdictに無いことではなくbroker snapshotによる不在証明が必要です。

household集中度はrisk計測のため同一instrumentを口座横断集約できますが、注文は1つのPositionIdentityに限定します。Kellyのsignal統計はticker + direction + horizon、position sizingは完全なPositionIdentityです。

## 4. 鮮度の権威

鮮度はfile全体でなく証拠単位です。

- sellは対象PositionIdentityのbroker照合済み数量が必要
- buyは対象account/currencyのfreshなcashが必要
- NISA actionはowner/account/type/tax year別のfreshなcapacityが必要
- `submitted`、`recommended`、内部`portfolio_applied`はbroker鮮度を進めない
- confirmed fillにはexternal execution ID、broker source/time、数量、価格、reconcile time、snapshot hashが必要
- 同じexternal execution IDの二重適用を拒否
- completeなbroker snapshotが1件もないlocal account/holding ledgerは`fresh`でなく`legacy_unverified`
- 旧executionのPATCH確認はPOST idempotency keyと別のbroker evidence schemaを使い、ledgerへ合成keyを作らない

分析入力の鮮度はholdings、cash、prices、FX、macro、news、screening、ticker別optionsに分離します。`fresh/degraded/stale/unknown`はsource timeと個別max-age policyから決まります。hashが証明するのは不変性で、鮮度ではありません。

更新境界と拒否境界は、1つのregistry内の別の値です。定期更新する判断入力は必ず`refresh_after_hours < stale_after_hours`を満たします。既定値はtechnical 4/8時間、macro 12/24時間、news 6/12時間、options 12/24時間です。契約testがregistry全体を走査するため、同値または逆転したsourceを追加するとCIで失敗します。macroのfallback/degradedとfreshでないoptions入力は、黙って残さずfrozen snapshot由来のheartbeatにも記録します。

## 5. Decision snapshot と execution snapshot

decision laneは2段階です。

1. `base_snapshot`: holdings、cash、prices、FX、macro、news、screening
2. `enriched_snapshot`: base + 保有、決定論的候補、open order銘柄のchart/options

最初のtier LLMより前にenriched snapshotを凍結します。source/retrieval time、freshness、source、artifact/payload hash、code revision、model ID、prompt hash、policy/config version、budget mode、tunable hash、analysis clockを記録します。同じsnapshot ID/hashをtier、final、action、policyまで維持します。

`execution_quote_snapshot`は別laneです。注文直前にprice、bid/ask spread、session、expiryを更新できますが、limit再計算またはreadiness降格だけです。元のthesis、confidence、expected returnを書き換えません。判断が変わるなら新analysis IDです。

## 6. LLM routing・構造化出力・privacy

`model_router.py`がroleを解決し、budget modeが解決後modelを昇降格します。通常の日次roleは次です。

- long、medium、保有swing: Claude Sonnet
- margin-long、short-sell: DeepSeek
- final synthesis: Claude Opus
- 低コスト抽出・検索・guard: Claude Haikuまたはprovider adapter
- 任意のpseudonymized judge: DeepSeek reasoner

sampling paramを拒否するAnthropic modelへ`temperature/top_p/top_k`を送りません。Opus 5/Sonnet 5は`output_config.effort=low`、adaptive thinkingとforced toolを使います。`max_tokens` truncateはerrorで、対象経路は上限を増やしてretryします。

analysis IDはrefresh、data gather、最初のmodel callより前に発行します。run contextはtierとRed Teamのworker threadへ複製し、計測済みLLM usage行はcallerが別の明示job IDを持たない限り同じIDを継承します。1分析のcostは時刻窓で推測せず`analysis_id`でjoinします。

actionの数量と金額は別々に保存します。株・口数actionの`amount_hint`は正規化した数量だけ、`estimated_notional_jpy`が金額の数値権威です。`22口 / 約¥76,164`のようなAPI/UI文字列はその構造化値から描画時に作ります。決定論的exit sizingではmodel confidenceを方向だけのconfidenceと明記します。最終数量はLLMでなくrule engineが決めるためです。

`ALMANAC_PRIVACY_MODE`がbook-aware送信を制御します。

| Mode | 許可するbook-aware provider |
|---|---|
| `strict_local` | なし |
| `anthropic_book_aware` | Anthropicのみ |
| `multi_provider_book_aware` | 設定済みprovider |

public/anonymized payloadは型allowlistと二次PII scanを通ります。public screenerは別のno-book call-site契約です。`run_with_secrets.sh`は`KEY=value`と`export KEY=value`の両方を子processへexportします。

## 7. Evidence lineage と claim

証拠はtagged unionです。

- `external`: URL、published time、observation date、retrieval time
- `snapshot`: artifact/payload hashとsource time
- `derived`: 入力claim IDとcalculation version
- `unverified`: 表示だけ

全actionにclaim IDを付けます。正しいlineageが無い確率・confidence数値はreadinessを下げます。GARCHやIV rankの内部指標に架空URLは付けず、入力claimと計算versionへリンクします。

tier由来のBlack-Litterman viewはtier出力と同じlineageです。名前を変えても独立根拠になりません。`independent_count=0`ならcorroboration文言を注入せずpriorを使います。

## 8. Candidate lane と採用

candidate生成はportfolio分析とは別です。

- momentum/fundamental screener
- long-term batch thesis生成と後日のbatch回収
- margin-long/short-sale screener
- news、social/options anomaly、pair、squeeze、overnight gap
- EDINET、TDnet、EDGAR disclosure feature
- insider、IPO、scenario playbook

各laneはuniverse、scanned、candidatesを別々に記録します。screen candidateはactionではありません。tier採用、final synthesis、account binding、policy/post-filter/readiness、invalidation確認を全て通る必要があります。

scenario playbookは事前合意した反応であり、policy bypassではありません。scenario statusとcapのattestationを持ち、execution-plan engineは完全な場合だけ専用経路を認めます。

## 9. Portfolio construction と Black-Litterman

optimizerは次をsupportします。

- `max_sharpe`
- `min_cvar`
- `equal_risk`（equal risk contributionではなくinverse-volatility）
- optional Black-Litterman

独立BL sourceはanalyst consensus、momentum、factor betaです。`BL_USE_INDEPENDENT_ALPHA=0`はtier由来rowを監査だけ、`1`は独立source、`mix`も消費するのは`is_independent=true`だけです。

optimization結果はtargetであってorderではありません。account eligibility、NISA、open intent、minimum lot、tax、readinessは後段で解決します。

## 10. Risk・volatility・model validation

現在保有weightと過去price returnからrisk seriesを再構成します。Cornish-Fisher VaRはskew/kurtosisでnormal quantileを補正します。主CVaRはhistorical Expected Shortfallで、Cornish-Fisher threshold版は補助、tail観測10件未満はunstableです。

稼働volatility modelはGJR-GARCHです。GINNは2-layer LSTM（hidden 64、dropout 0.1）+ linear + Softplusで、lossは次です。

`MSE(predicted sigma, absolute residual) + 0.3 × MSE(predicted sigma, GARCH sigma)`

GINNはversion bundleが固定promotion policy、model/manifest checksum、data age、feature coverage、validation件数/銘柄数、GARCH比較、inference schemaを通るまで研究用です。manifest/current pointer欠落はdefault-denyし、理由付きGJR-GARCH fallbackを返します。flat legacy modelは監査用に残しますが暗黙loadしません。

per-ticker scalerの永続化inference契約は実装済みです。candidateは契約completeとper-ticker scaler artifact/checksumを持ちます。ただしこれは昇格条件の一つです。固定validation/GARCH比較、freshness、coverage、その他manifest gateのどれかに失敗すればcandidateは拒否されます。VIX/regime履歴とleakage-free rolling GARCH featureは将来研究です。昇格に使ったheld-outはvalidationであり、昇格後に到着した観測だけがforward evidenceです。

現在のforward fieldはschema予約だけで、forward評価pipelineは未実装です。保存値0を実績として表示しません。runtime statusは実際の稼働modelと最新の監査候補を別fieldで示します。

VaR予測を保存してKupiec proportion-of-failures testを行います。合格しても検証されるのはそのVaR seriesのbreach頻度で、risk stack全体ではありません。

## 11. 5段階market regime・金利・現金

`market_regime_v2.py`はUS/JPを別々に採点し、invested equity valueでportfolioへ合成します。

| Level | Cash target | New-buy multiplier | Leverage |
|---|---:|---:|---|
| 強い強気 | 3% | 1.00 | 条件付き |
| 弱い強気 | 7% | 0.75 | 不可 |
| 中立 | 12% | 0.50 | 不可 |
| 弱い弱気 | 20% | 0.25 | 不可 |
| 強い弱気 | 30% | 0.00 | 不可 |

indexのMA50/MA200乖離、breadth、VIX、HY OAS、金利を使います。金利はtightening shock、easing support、stress easing、restrictive real/nominal level、curve inversionを分けます。coverage、breadth件数、risk/rate入力がeligibilityを満たす必要があります。2評価確認で単発noiseによるcommitted level変更を防ぎます。

別のshock overlayは裁量buyを止められますが、crash後にcash targetへ戻すための売却はしません。確認済みcashは全て余剰投資資金、生活防衛reserveは0ですが、tactical cash、settlement、collateral、fee、tax、既存order予約は残ります。execution planはtactical targetを超える確認済み残高を、強い強気／弱い強気／中立／弱い弱気では2／3／6／12か月に分けて月次枠を作り、強い弱気またはshock中は通常買付枠を作りません。当月の約定は枠を消費しますがcash basisを二重に縮めず、確認済みcash flowは自動再計算します。cash authorityは時間経過だけで失効させず後続eventで判定します。

household cashは配分・tactical reserve計算では集約できますが、実行可能walletは1つにまとめません。最終候補はowner・broker・settlement pool・currency別の確認済み買付余力も予約します。owner間transferやFX conversionは別の確認済みeventが必要で、household totalから暗黙推定しません。

## 12. Policy・readiness・invalidation

`policy_engine.py`は決定論的です。主なcheckはdrawdown/VaR/VIX、regime size cap、leverage、NISA/account、earnings/macro blackout、order-intent conflict、discretionary funding、concentration、minimum unitです。

`risk_policy.py`を、portfolio guardrailの唯一のversion管理された権威とします。日次P&L、30日rolling P&L、peak drawdownは別metricであり、値の大きさから単位を推測したり、P&L proxyをdrawdownと呼び替えたりしません。固定loss shock水準は日次-3%、30日-6% / -9% / -12%です。ex-ante 1日95% VaR budgetはstress 1.2%、通常1.4%、confirmed bullかつVIX<25だけ1.6%、絶対上限1.8%です。発注時の単一銘柄集中度は8%からreview、10%でrejectします。過去にAI tuningされたrisk値は監査履歴にだけ残します。

flow-adjusted drawdownはshadow seriesから始めます。60 effective NAV日、60 forward shadow effective日、cash-flow coverage 95%以上、invalid/estimated inputなし、手動reconciliation記録、を全て満たした後だけ人が昇格できます。昇格後は-5% / -8% / -10% / -12%で即時悪化し、-15%はobjective breachです。回復は-3% / -6% / -8% / -10%を上回る状態が5 effective NAV日続いたときに一段だけ進み、`freeze`解除には記録付き人承認も必要です。-10%状態は戦術・投機risk budget 50%のde-risk案を人reviewへ送り、自動size乗数や自動清算にはしません。

readinessは加算的でseverityは単調です。後段checkが`blocked`を`review/ready`へ改善できません。reasonは構造化して保存します。stale/unknown position、unverified claim、ambiguous account、未解決sizing、直近eventは契約に従い降格/拒否します。

riskを増やす注文を記録する直前に、`POST /api/actions/preflight`が現在のloss guard、regime別VaR budget、注文後集中度、昇格済みdrawdown stateを決定論的かつread-onlyで再評価します。署名済み結果は60分有効で、ticker、direction、order type、quantity、price/limit、currency、account/route、action identityに結び付きます。通常の閾値超過は理由を表示して人の明示確認を記録し、VaR 1.8%、集中度10%、またはidentity変更はhard rejectです。指値再計算はこのcheckだけを再実行し、朝のAI分析を増やしません。sell、cover、hedge、stopは通します。`executed`と`partial`は既発生の事実なので拒否せず、preflight欠落を`reported_without_preflight`として残します。

`execution_invalidation_state.json`は過去analysis/actionへの不変overlayです。Today、API、backlog、notification、action-state consumerが同じresolverを使います。原文履歴は消さず、旧actionの実行・dedup復活を防ぎます。過去fill報告は可能です。

## 13. 決定論的sizingとorder intent

exit数量は次の順です。

`current weight → target/band → maximum step → tax effect → lot rounding → open orders`

`intent_key`は経済的意図の重複防止、`evaluation_key`は1 snapshot評価の識別です。snapshot変更は同じintentのrevisionで、数量を累積しません。既存orderがあればcancel/replaceか人確認です。tax不明は0数量でなく`review`です。

execution quoteはbid/ask、spread、ATR、VWAP、support/resistance、expiry、market/limit可否を計算します。no-trade bandはexpected edgeとspread、fee、実測implementation shortfallを比較します。説明文は構造化結果から生成します。

action stateはrecommendation、pending、placed、ordered、filled、cancelled、expired、invalidatedを分けます。Position freshnessを進めるのはbroker-confirmed fillだけです。

## 14. DCAと集中上限

drawdown ladderのtriggerは独立です。

- T1: VIX peakからの減衰
- T2: DD≤-12%、VIX≥25、Fear & Greed≤25、HY OAS≥500bps
- T3: DD≤-18%、VIX≥40、put/call>1.2またはVIX>40、かつRSI reversal

全trancheにbreadth、volume、cooldown、年間cap（純資産15%）、currency別funding checkがあります。

household集中度はcanonical instrumentでowner/broker/account横断集約します。default capはlong 10%、medium 5%、swing 2%、勤務先株10%。tier混在は最も厳しいtierを使いreview表示します。shadow-onlyでactionを変更しません。

## 15. Half-Kelly影実行

Half-Kellyは次です。

`0.5 × (p × b - q) / b`

sampleはtrade実績ではなく推薦の事後成績です。buy/add/DCA、analysis+ticker+directionでdedup、5/20/60営業日horizon分離、要求horizonで最低20観測です。execution不能でもsignalは評価可能な場合がありますが、stale/unknown inputはsignal自体を評価不能にします。

capはabsolute target positionで`max(0, Kelly target - current holding)`です。counterfactualは実経路と同じfrozen contextから始め、policy前にcap、rounding/filterを再実行して差を記録します。action state、recommendation log、execution plan、notification、実actionを書きません。1unit未満はassertで落とさずnon-actionableです。

## 16. Economic currency と FX影実行

3つの量を分離します。

- gross asset currency allocation
- hedge控除後net FX exposure
- hedge overlay notional

listing currencyはeconomic currencyではありません。instrument masterはunderlying exposure、USD ratio、hedge ratio、leverage/inverse、replacement eligibility、source、as-of、validityを持ちます。不明・期限切れは0 USDでなくfail-closedです。

actual hedge notionalはcompleteなbroker position snapshotだけから読みます。証拠欠落は0でなく`unknown`です。shadow/target/actual stateは別です。target ratioは0–70%、1評価日の変化は±10 percentage points、同日同snapshot再実行は冪等です。

FX forward・spot FX・currency futureと識別された後続fillは、completeなbroker snapshotで置換されるまで旧actual-notional観測を失効させます。通常の外国株fillは失効理由にしません。

hedged ETFはoverlayでなく対応unhedged index exposureの置換で、tax/cash/lot check後だけです。future/FXはmargin・tax・order adapterが別のoverlayです。modeは`off/shadow/advisory`で、自動発注はありません。

## 17. 税務・NISA・持株会

tax v2 schemaは常に次を分離します。

- economic realized P&L
- taxable realized P&L
- NISA realized P&L

`ALMANAC_TAX_BASIS_MODE=legacy|compare|total_average`はAPI fieldを変えず、既定は`total_average`です。税務fieldへ値を入れられるのはcompleteなtotal-average-like結果だけで、`legacy`と`compare`はFIFOを監査比較として残しても権威へ無言昇格しません。engineはowner/broker/account/instrument別にfee、FX、split/merge、transfer、opening balanceを扱い、不完全historyでは税務値を明示的にunavailableにします。broker税務値があれば表示の権威、内部ledgerは照合値です。

NISA損益を課税口座と通算しません。年間投資枠と翌年復活する生涯保有限度額は別です。migrationはowner/brokerを跨げません。lot IDは監査lineageだけで、税額はposition平均basisを使います。

`tax_lot.py`は数量、欠落取引、corporate action、broker単価の監査に残します。日本の総平均契約ではspecific-lot loss-harvest/gain-minimize提案をactionableにしません。持株会moduleは集中、奨励金、人だけが行うexit logicを別管理します。

## 18. 会計・成績・governance

`event_ledger.py`はappend-onlyでtrade、cash flow、dividend、fee、tax、FX eventを記録します。broker reconciliationは外部記録とledgerを比較します。訂正は破壊編集でなく新eventです。

daily NAVとbenchmarkは別seriesです。performanceはModified Dietz cash-flow-adjusted近似で、税引後・fee後・JPY成績を60/40 benchmarkと比較します。estimated NAV backfillはchartを補いますがdaily guardrailのmeasured anchorから除外します。

recommendation outcome、sell/catalyst outcome、execution quality、action-stage coverage、agent attribution、monthly governanceは別metricです。candidate、policy accepted、final action、filled、measured outcomeを1つの成功率に混ぜません。

## 19. 自動実行・state・故障時挙動

公開repoはscheduleをinstallしません。example LaunchAgentはAI分析、disclosure、NAV、benchmark、example crontabはscreenerです。commandはabsolute interpreterまたは`venv/bin/python`を使い、`run_with_secrets.sh`経由、既定Asia/Tokyoです。

state writerは用途に応じatomic replaceまたはappend-onlyです。testはstate/model/report dirをtmpへ向けます。Python monkeypatchはsubprocessを越えないためCLI testには環境変数levelのdirectory overrideが必要です。

故障契約:

- stale/missingは可視化しreview/block、暗黙clearにしない
- GINN failureは実model label付きGJR-GARCH fallback
- 任意model/lane失敗はそのlaneだけdegrade
- final truncate/malformed tool outputは失敗
- post-filter failureはactionをquarantine
- shadow stateはactual holdingsを進めない
- additive tax migrationのrollbackはread flag
- unvalidated GINN safety gateをlegacy loadへrevertしない

## 20. API・dashboard・検証・公開

FastAPIはread viewと認証付きwrite controlを提供します。Todayは表示前にinvalidation/readinessを解決します。fill endpointはbroker evidenceを記録しますが注文しません。tax APIはmodeによらずv2です。Next.js dashboardはconsumerであり第二の権威ではありません。

最低release手順:

```bash
python3 -m py_compile <changed Python files>
venv/bin/python -m pytest tests/ -q
python scripts/check_docs_consistency.py
python scripts/check_public_safety.py
git diff --check
```

日次経路の変更ではlive `portfolio_analyst.py --force`、続けて`post_run_verify.py`を実行し、そのanalysis IDの新規logだけを監査します。model ID、tool stop reason、cost、snapshot hash、readiness reason、provenance、GINN fallback/promotion、FX/Kelly shadow、state mutation allowlistを確認します。

公開repoへ入れるのはcode、fixture、example configだけです。broker export、household identity map、本番model bundle/manifest、FX actual/shadow state、tax比較、crontab backup、local path、state hash manifestを入れません。責務境界は完全な[モジュール台帳](MODULE_CATALOG.ja.md)を参照してください。

## 21. レビュー用artifact map

この表はfilename一覧ではなく権威の地図です。reviewでは結論を出す前に、producer契約、consumer、raw record/derived result/overlayの別を確認します。

| Artifact | 権威と用途 | review時の規則 |
|---|---|---|
| `ai_portfolio_analysis.json` | 最新の統合分析結果 | 利便表示であって過去の証明ではありません。過去判断は`analysis_id`を固定し、stage logから再構成します。 |
| `action_stage_log.jsonl` | candidate/action stageのappend-only監査 | `tier_generated`からsynthesis、policy、post-filter、readinessまで追います。candidate数をtradable action数と同一視しません。 |
| `decision_snapshot_state.json` | frozen base/enriched inputとhash | hashは再現同一性でありfreshnessではありません。input別のsource/as-of/freshnessを別に確認します。 |
| `action_state.json` | recommendation/intentの可変lifecycle | local recommendation stateでありbroker truthではありません。実行可否の前にinvalidation overlayを読みます。 |
| `execution_invalidation_state.json` | 旧analysis/actionを無効化する不変overlay | 原文は残し、Today/API/backlog/notification consumerに優先します。 |
| `action_executions.json`とreconciliation state | 人/ broker evidenceによるexecution記録と訂正overlay | API fill recordはbroker gatewayではありません。freshnessはbroker-confirmation evidence契約を通る場合だけ進みます。 |
| broker position/cash snapshot | holdings/cash/capacityの外部根拠 | 適切なidentityでscopeします。別銘柄の更新で対象positionをfreshにしません。 |
| `models/ginn/current.json`とbundle manifest | 昇格可能modelとvalidation契約へのpointer | 有効なcurrent pointerのないlegacy/candidate fileは監査用で、稼働modelではありません。 |
| `tunable_params.json`と`tunable_params_history.jsonl` | 現在parameterとappend-only履歴 | active value、authorizing mode、historyを合わせて読みます。過去rowは必ずしもactiveではありません。 |
| currency/FX shadow state | broker-confirmed actual hedgeとは分離したpolicy結果 | shadow proposalは実ヘッジやactual notionalを証明・更新しません。 |

本番stateにはhousehold/broker情報があり公開artifactではありません。公開fixtureはschema例であり、実portfolioの根拠ではありません。

## 22. 書込み権威と注文境界

`POST`、`PUT`、`PATCH`、`DELETE`はlocal state mutationでありbroker注文ではありません。すべてのwrite routeが`X-API-Key`を要求し、実行時に認証を迂回する環境変数はありません。read-only routeはkeyなしで利用できます。

| 書込み分類 | 変更し得るもの | 意味してはならないもの |
|---|---|---|
| analysis/screen/scenario refresh | derived analysis、snapshot、candidate artifact | broker order/fill/cash movement |
| user-reported execution/fill/reconciliation | local ledger、execution record、条件を満たすfreshness evidence | credentialed broker routing。呼出元が既に得たevidenceを記録するだけです。 |
| cash/holdings/capacity reconciliation | 必要evidence確認後のlocal accounting/reconciliation state | unknown balanceが0、または別owner/accountも更新されたこと |
| feature/configuration control | 独立して認可されたlocal switch/parameter | policy bypass、execution approval、遡及的な証明 |
| correction/rollback | 新しいcorrection、invalidation、read mode | 元監査eventの破壊的書換え |

公開API routeに注文をsubmitする契約やbroker order routingはありません。強制可能な境界は手動発注前のpreflight表示とlocalの`status=ordered`遷移までで、外部broker画面へ直接入力された注文は止められません。implementation reviewではbutton表示やHTTP verbで推測せず、endpointとbroker-client境界から確認します。System UIはこれらcontrolのoperator viewであり、第二の権威ではありません。

## 23. 文章から始めるreview手順

sourceを読む前にこの仕様だけでclaimを検証可能にするための手順です。

1. claimを明確化します。model選択、candidate、readiness、数量、tax、FX、UI表示、mutationのどれかを区別します。
2. `analysis_id`を固定します。過去判断をlatest outputだけで説明しません。
3. input authorityとidentity scopeを特定します。positionはowner/broker/account/instrument、cashはcurrency、NISAはtax year/typeまで見ます。
4. freshnessとimmutabilityを分けます。frozen snapshotでもstale inputを正確に保存できます。無関係なholding更新は対象をfreshにしません。
5. stage logでcandidate funnelを追います。screen済み銘柄を全てactionと呼びません。
6. model/evidence lineageを確認します。derived claimにはinput claim IDとcalculation versionが必要で、同一tier出力由来viewは独立根拠ではありません。
7. synthesis後の決定論的policy/readinessを確認します。後段は`blocked`を`ready`へ改善できません。
8. 表示数量を実行可能と扱う前にsizingとopen-intentの冪等性を確認します。
9. write境界と全consumer（Today/API/backlog/notification/state）を確認します。local recordをbroker executionと説明しません。
10. 発見をcontract testにし、20節のrelease/public-safety checkを実行します。

重要な不変条件は、unknownは0ではない、candidateはactionではない、snapshot hashはfreshnessではない、invalidationは履歴を消さずoverlayする、shadow/advisory outputはactual holdingを更新せず注文しない、です。

## 24. 現在の限界と文書保守

次は将来機能の主張ではなく、現在の契約を明示したものです。

| 領域 | 現在の契約 | 拡張した主張/有効化の前提 |
|---|---|---|
| GINN | GJR-GARCHが稼働します。GINNは永続scaler/inference契約を持ちますが、固定validation・比較・manifest gateを通るまでdefault-denyです。forward fieldは予約済みですが評価pipelineは未実装です。 | historical VIX/regime、leakage-safe rolling GARCH input、baselineに対する安定validation、forward evaluator実装、昇格後の十分なevidence。 |
| Half-Kelly | recommendation performanceに基づくshadow/counterfactual outputで、trade performanceの証明でも注文でもありません。 | 固定evaluation population/horizon、十分な観測、shadow recordの人確認。 |
| FX | classification、target、actual hedge evidenceを分離し、modeはoff/shadow/advisoryで自動注文しません。 | complete economic-exposure分類、broker-confirmed actual notional、vehicle別の運用/tax control。 |
| Tax | API schema v2はeconomic/taxable/NISA P&Lを分離し、broker税務値が権威です。 | 完全な照合期間とconsumer確認後にlegacy read pathを廃止。 |
| Execution plan | readiness/invalidationが権威で、UI recommendationはbroker instructionではありません。 | 必須position/cash/NISA evidence、決定論的sizing、人確認。 |

source契約を変えるときは、この英語仕様、対応する日本語仕様、README要約、UI文言、documentation contract testを同じ変更で更新します。時間で変わる運用stateは永続README断言でなくSystem UI/APIまたはreview済みartifactに置きます。これにより文章だけのreviewでも設計不明点と実装/文書の不一致を発見できます。
