# ALMANAC

*[English](README.md)*

**ALMANAC** は、個人のポートフォリオ運用を支援するAIアシスト型の資産管理・リスク管理システムです。Pythonバックエンドと Next.js ダッシュボードを組み合わせ、実際の長期投資口座に対して日次のポートフォリオ分析・銘柄スクリーニング・規律あるリスク管理を行います。AIの提案と実際の発注の間には、必ず決定論的なガードレールが挟まる設計です。

**自動売買botではありません。** このコードベースのどこにも証券会社への発注APIはありません。AIが提案し、ポリシーエンジンがその提案を通すか止めるかを判定し、実際の発注は人間が証券会社の画面で行います。

このリポジトリは、そのシステムの**公開用に匿名化したスナップショット**です。実運用データ・認証情報・保有者を特定しうる情報は意図的に除外しています（詳細は [公開リポジトリの安全性](#公開リポジトリの安全性public-repository-safety)）。

> **プロジェクトの位置づけ:** これはターンキー製品や安定した公開APIではなく、明確な設計思想を持つリファレンス実装であり、進化中の個人システムです。まずデモ状態で試し、ルールを確認し、ファイルスキーマや運用手順が変わりうる前提で利用してください。

## できること

目的関数は明文化されバージョン管理されています（[`objective.md`](objective.md)）：**税引後・手数料控除後・円建ての時間加重収益率（TWR）を、グローバル株式60%／グローバル債券40%のベンチマークに対して最大化する**こと。これはVaR・ドローダウン・VIX連動のサーキットブレーカーといったハードな制約下にあり、これらはLLMの判断ではなく決定論的なポリシーエンジンが強制します。

| 領域 | 内容 |
|---|---|
| **ポートフォリオ・リスク** | LLM生成ビューを用いたBlack-Litterman最適化、GJR-GARCHによるボラティリティモデリング、相場レジーム判定（強気／中立／弱気／クラッシュ）、集中リスク・人的資本エクスポージャーの上限管理 |
| **AI判断支援** | Claude + DeepSeekによるマルチモデル分析（タスクごとにコスト最適なモデルを選択）。トリム・買い増し・リバランス・損出しといったケース別判断を、発注に至る前に必ず決定論的ポリシーでゲーティング |
| **スクリーニング・シグナル** | 日米のファンダメンタルズ長期スクリーニング、開示（EDINET／TDnet／EDGAR）起点のカタリスト検知、信用・空売り候補スクリーニング、インサイダークラスター・IPO監視 |
| **執行・ガードレール** | 日次／月次ドローダウンのサーキットブレーカー、VaR・VIX連動の発注ブロック、監査用のappend-onlyイベント台帳、既存注文を考慮したポジションサイジング |
| **税務・口座管理** | FIFO/LIFO/損出し/利益最小化の税ロット戦略、NISA枠の追跡、持株会（従業員株式制度）の集中度管理 |
| **可観測性** | ベンチマーク対比のNAV/TWR実績追跡（Modified Dietz法によるキャッシュフロー調整済みの近似値。日次sub-period計算による厳密なTWRではない）。固定的な実績主張ではなく、実測値をそのまま示す検証ページ |

## 動作原理

このシステムの中心は、市場データを少数の「人間が実行できる具体的な提案」に変換する日次パイプラインと、**ユーザーに届く前に提案を reject / modify できる決定論的なゲート**です。

### 1. 日次ループ

```mermaid
flowchart TD
    A["鮮度保証<br/>マクロイベント · テクニカル · VIX · 決算 · シナリオ"] --> B["データ + 文脈収集<br/>ポジション · 価格 · 為替 · ニュース · 触媒"]
    B --> C{"5ティア分析<br/>（並列）"}
    C --> C1["Long / Medium / Swing<br/>Claude Sonnet"]
    C --> C2["信用買い / 空売り<br/>DeepSeek V4 Pro"]
    C1 --> D["Red Team<br/>Claude Haiku · DeepSeek · Groq · Gemini · Qwen"]
    C2 --> D
    D --> E["エージェント間不一致スコア<br/>+ Black-Litterman ビュー"]
    E --> F["任意のJudge<br/>DeepSeek-R1"]
    F --> G["最終合成<br/>Claude Opus"]
    G --> H["決定論的な後処理<br/>経路 · サイズ · 指値文脈"]
    H --> I{"Policy Engine<br/>決定論的ゲート"}
    I -->|reject| J["理由付きで記録<br/>アクションとしては表に出ない"]
    I -->|accept / modify| K["action_state.json<br/>+ 推奨ログ"]
    K --> L["ダッシュボード + Telegram<br/>人間が判断して発注"]
```

各段階には理由があります。

**まず鮮度を保証する。** ゲートが依存する入力 — マクロイベント暦、テクニカル状態、VIX、決算接近、シナリオスナップショット — は分析開始**前**にすべて再生成します。古い暦をそのまま読むと「重要イベントなし」と黙って解釈され、決算ブラックアウトが発動するかしないかの差になるからです。更新失敗は握り潰さず出力し、暦の欠落は「問題なし」ではなく `review` として扱います。

**汎用1体ではなく専門5体。** ポートフォリオを保有意図（長期コア / 中期 / スイング）で分割し、さらに信用買い・空売りの2レーンを加えます。各々が固有のプロンプトと固有のリスク語彙を持ちます。並列実行され呼び出し単位でタイムアウトを持つため、1ティアの失敗はそのレーンの劣化に留まり、実行全体は落ちません。

**敵対的レビュー。** ティア出力は**別系統のモデル**による Red Team に渡され、推論を攻撃させます。Claude Haiku のレーンはbook-awareな文脈を扱えます。外部ベンダーのレーンは公開情報または匿名化情報だけを使い、キーが設定されていれば DeepSeek / Groq / Gemini / Qwen を利用します。ベンダーを分けるのは意図的で、同系統のモデルは盲点を共有しやすいためです。エージェント間の不一致スコアを算出して後段に引き継ぐので、合意だけを見て分岐点を見失うことがありません。

**任意のJudge、そして合成。** `DEEPSEEK_API_KEY` が設定されている場合、DeepSeek-R1 が、銘柄記号とアナリストの自由記述理由を受け取らない匿名化アクションを裁定します。この任意ステージが利用できなくても、実行全体を停止せず省略します。その後 Claude Opus が構造化された結果に最終合成します。合成呼び出しは forced tool use を使うため、出力はパースが必要な散文ではなく検証済みオブジェクトです。トークン上限で切断された応答は「部分的な答え」として採用せず、明示的に拒否します。

**判断材料は合成前、執行詳細は合成後。** ニュース・触媒・チャート・オプションの文脈は、判断に影響する段階で合成前または合成中に収集されます。構造化された提案が戻った後は、決定論的コードが経路・サイズ・指値文脈を付加してからポリシーゲートへ渡します。

### 2. なぜ複数モデルなのか

主要なロールベースのモデル選択は `model_router.py` に集約されています。`ALMANAC_BUDGET_MODE=eco|normal|premium` は、ロール解決後のClaudeティアを変換します。ただし、低リスクの固定ユーティリティ呼び出し、フォールバック用モデルID、外部プロバイダのロールまでは書き換えません。これらの例外は各呼び出し箇所に明示されています。

| ロール | モデルティア | 理由 |
|---|---|---|
| 最終合成 | Claude Opus | 誤りが全提案に伝播する唯一の呼び出し |
| Long / Medium / Swing | Claude Sonnet | 量は多いが品質も要る本体分析 |
| 信用買い / 空売り | DeepSeek V4 Pro | 信用側の一次判断。採否は最終合成が決める |
| スクリーナー予選 | DeepSeek | 多数の候補を広く安く一次通過させる |
| スクリーナー第二意見 | Claude Sonnet | BUY上位だけが高価な精査を受ける |
| Red Team | Claude Haiku / DeepSeek / Groq / Gemini / Qwen | book-awareなAnthropicレーンと、公開・匿名化したクロスベンダー批判 |
| チャット / 差分監視 | Claude Haiku | 高頻度・低リスク |

経済的な形はファネルです。安いモデルが全部を見て、高いモデルは生き残ったものだけを見る。全呼び出しはトークン使用量と推定コストを共有台帳に記録するため、支出は「たぶんこのくらい」ではなく実測値になります。

### 3. ゲート

**ここが「トレードを提案するLLM」との分かれ目です。** 提案されたアクションは、順序付きの**決定論的ルール連鎖**を通ります。素のPythonで、この経路にモデルは介在しません。ルールはアクションを reject するか、modify（緊急度の降格・サイズ半減）できます。

| ルール | 内容 |
|---|---|
| `ledger_integrity` | 台帳が不整合なら実行可能なアクションを通さない（fail-closed） |
| `var_budget` | 事前1日95% VaR が予算超過 → 新規買い**全て** reject |
| `dd_stage` | ドローダウン ≤ −8% → 原則として新規買い停止 / ≤ −5% → 緊急度降格 + サイズ半減。決定論的なDCAラダー例外は別途上限管理 |
| `leverage_block` | レバレッジが warning/deleverage/emergency → 新規信用建て禁止 |
| `earnings_blackout` | 決算5営業日以内 → 原則として buy / add / DCA を reject。明示的な高確信度イベント取引の例外は後段で上限管理 |
| `freshness_downgrade` | 入力が古い → 信用せず降格 |
| `cvar_unstable` | 実テールサンプル不足は信用買いをhard block。クリーン履歴不足は恒久ブロックせずサイズを縮小 |
| `vix_extreme` | VIX ≥ 40 → 投機系を reject、buy の緊急度を降格 |

個々の閾値より重要な設計判断が2つあります。

- **fail-open ではなく fail-closed。** 入力の欠落や読み取り失敗は「異議なし」ではなく「許可しない」として扱います。複数のルールが `False` と `None` を明示的に区別しているのはこのためです。
- **却下は捨てずに記録する。** reject / modify されたアクションは理由とともに分析結果に書き出されるため、ゲートの挙動は事後監査できます。「期待した売買がなぜ出てこなかったか」を後から問えます。

既定の閾値は、何を最適化するかを定義したバージョン管理下の [`objective.md`](objective.md) を実装する意図で設定されています。制限を変える場合は、目的文書・ランタイム設定・コード・回帰テストを同期させます。

### 4. 提案から約定まで

**このリポジトリに証券会社の発注APIはありません。** ループは人間を経由して閉じます。

```
提案 → readiness 判定（ready | review | blocked） → 人間が証券会社で発注
     → 人間が約定を記録 → executed | partial → イベント台帳 → ポートフォリオ反映
```

約定の記録とポートフォリオへの適用は意図的に分離しています。口座・経路が一意に確定できない約定は、**起きた事実として保存**したうえで `portfolio_application_pending` として保留し、推測で誤った口座に書き込みません。帰属を誤ると、税務ロット・NISA枠・パフォーマンス数値のすべてが静かに壊れるからです。書き込みはクライアント生成キーで冪等化されており、フォームの二重送信が2件の約定になることはありません。

### 5. パフォーマンスの測り方

このシステムは結果を主張するのではなく、自分を採点します。日次で NAV を記録し、時間加重収益率（サブ期間厳密なTWRではなく、キャッシュフロー調整の Modified Dietz 近似）を、世界株式60% / 世界債券40% のベンチマークに対して算出します。目的関数は**税引後・手数料後・JPY建て** — 国内分離課税と米国配当源泉税をモデル化し、USD建ては日次終値で円換算します。

ダッシュボードの検証ページは、実測値そのものを報告します。計測期間が短すぎる・汚染されていて結論を出せない場合はそう表示します。別途、watchdog がデータ鮮度・スキーマ変化・台帳整合性・バックアップ状態・ディスク残量を定期チェックし、本当に対処が必要な問題だけを通知します。

### 6. 壊れたときの挙動

劣化は暗黙にせず明示します。タイムアウトしたティアは実行を degraded として出力に明記し、切断されたLLM応答はパースせず拒否し、古い入力は信用せず降格させ、安全モジュールを import できなければ無監査で進まず呼び出しを拒否します。一貫している原則は、**自信のある誤りを出すくらいなら何も推奨しない**ことです。

## アーキテクチャ

- **バックエンド** — Python 3.12 / FastAPI。ポートフォリオ最適化（[PyPortfolioOpt](https://github.com/robertmartin8/PyPortfolioOpt)、[riskfolio-lib](https://riskfolio-lib.readthedocs.io/)、[skfolio](https://skfolio.org/)）、GARCHリスクモデリング（[arch](https://arch.readthedocs.io/)）、FinBERTセンチメント分析（`transformers` / `torch`）、AI分析にClaude（Anthropic）とDeepSeekを使用。
- **フロントエンド** — Next.js 16（App Router）/ React 19 / TypeScript。ポートフォリオ・スクリーニング・リスク・シナリオ・戦略・信用取引・NISA・AI判断支援・執行ログ・パフォーマンス検証ページを1つのコンソールに統合。
- **プライバシー層** — ALMANACはローカルで動作しますが、設定されたAI機能の一部は、保有銘柄・数量・損益・配分などのポートフォリオコンテキストを外部LLMへ送信します。公開・匿名化データだけを扱う非Anthropic経路（開示特徴量抽出・討論・外部Red Team・スクリーニング）は、許可リスト方式のゲート（`almanac/llm_safety.py`）を通ります。book-aware経路にはティア/最終分析・チャット・判断支援・ガードレール通知・Anthropic Red Teamが含まれます。大部分は呼び出し箇所単位のprivacy gateで制御されますが、現在のRed Team例外は下記に明記しています。

## 設定（Configuration）

`.env.example` は設定テンプレートとして使います。CLI分析のシークレットは、プロセス環境または `run_with_secrets.sh` 経由の `~/.almanac_secrets` から渡します。FastAPIの書き込み認証は、これとは別に `ALMANAC_API_KEY` または `~/.config/almanac/api_key` を読み込みます。プロジェクト直下の `.env` は**読み込みません**。コードの閲覧、read-only API、デモダッシュボードの確認だけなら設定は不要です。

**対応するAIワークフローを使う場合のみ必須**

| 変数 | 用途 |
|---|---|
| `ANTHROPIC_API_KEY` | Claude — AI判断支援・ケース分析・LLM生成ポートフォリオビューの中核 |
| `DEEPSEEK_API_KEY` | DeepSeek — コスト効率重視のスクリーニング・長期スキャン処理 |

**任意**

| 変数 | 用途 |
|---|---|
| `FRED_API_KEY` | マクロ経済データ（FRED）— レジーム判定・リスク文脈に使用 |
| `FINNHUB_API_KEY` | 補助的な市場データ |
| `GEMINI_API_KEY`, `GOOGLE_AI_API_KEY` | 代替LLMバックエンド |
| `GROQ_API_KEY` | 高速推論の代替LLMバックエンド |
| `OPENROUTER_API_KEY` | LLMルーティング／代替バックエンド |
| `TELEGRAM_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | アラート・日次ブリーフィングのプッシュ通知 |
| `ALMANAC_API_KEY`, `NEXT_PUBLIC_ALMANAC_API_KEY` | 書き込み系エンドポイント（取引記録・チューニング変更）の認証キー。閲覧のみなら不要 |
| `ALMANAC_ESPP_*` | 持株会（従業員株式制度）追跡設定。既定は全て無効（`0`） |
| `ALMANAC_CONTRIBUTION_SCHEDULE_JSON` | 定期積立の設定。既定は空 |
| `ALMANAC_CLEAN_NAV_SINCE`, `ALMANAC_MIN_CLEAN_DAYS` | パフォーマンス計測期間の衛生設定 |
| `ALMANAC_PRIVACY_MODE` | 呼び出し箇所単位でゲートされたbook-aware外部LLM呼び出しを制御する。対象範囲と現在の例外は下記 |
| `ALMANAC_BUDGET_MODE` | Claudeのルーティング方針: `eco` / `normal`（既定）/ `premium`。固定ユーティリティ呼び出しと外部プロバイダのロールは変えない |
| `ALMANAC_MODEL_OVERRIDE_<ROLE>` | 制御されたテストやロールバック向けのロール別ルーティング上書き。値はプロバイダのモデルIDではなく `sonnet` などのレジストリキー |

### プライバシーモード

一部のAI機能は、設計上ポートフォリオコンテキスト（保有銘柄・残高・損益）を外部モデルへ送信します（該当箇所は [公開リポジトリの安全性](#公開リポジトリの安全性public-repository-safety) を参照）。`ALMANAC_PRIVACY_MODE` は、それらの呼び出しをそもそも実行してよいかを制御します。

| 値 | 効果 |
|---|---|
| `strict_local`（既定） | ゲート済みのbook-aware経路 — ティア分析・チャット・判断支援・ガードレール通知・最終統合 — はプロバイダ呼び出し前に遮断され、ローカルの無効化/エラー結果を返す |
| `anthropic_book_aware` | Anthropicへのbook-aware呼び出しのみ許可 |
| `multi_provider_book_aware` | 設定済みの全プロバイダへのbook-aware呼び出しを許可（このコードベースの元々の、ゲート導入前の挙動） |

公開・匿名化データの呼び出し（スクリーニング・開示特徴量抽出）はこの設定の影響を受けません。そもそもポートフォリオ情報を含まないためです。`assert_book_aware_allowed()` でゲートされている呼び出し箇所は `tests/test_llm_call_site_gating.py` の回帰テストで列挙されています。

> **重要な実装上の境界:** privacy mode は呼び出し箇所単位のポリシーであり、プロセス全体のネットワークsandboxではありません。このスナップショットでは、Claude Haiku の Red Team レーンが共通Claude transport経由で保有情報を含むプロンプトを構築しますが、まだ `assert_book_aware_allowed()` の対象外です。非Anthropicの Red Team レーンは引き続き公開情報のみを扱います。この呼び出しがゲートされるまでは、`strict_local` を「全てのAnthropicリクエストを必ず遮断する証明」とみなさないでください。外部送信を完全に止める実行では、外部APIキーを設定しないか、ネットワーク隔離を併用してください。

## 公開リポジトリの安全性（Public Repository Safety）

このリポジトリは、ローカルのポートフォリオ状態・証券会社からのエクスポート・データベース・ログ・スクリーンショット・ローカルAIツールのセッション・APIキーを意図的に含んでいません。

`holdings.json`・`account.json`・`nisa_portfolio.json`・`trade_history.csv`・`almanac.db` などはGitで無視され、ローカル環境の外に出ることはありません。ドキュメント中の数値例は実際の金額ではなく、丸めたプレースホルダーを使用しています。`scripts/check_public_safety.py` は、**現在トラッキングされているスナップショット**から既知の個人識別情報やシークレットキーのパターンを検出します。Git履歴は検査せず、専用のsecret scannerを代替するものでもありません。push前に実行してください。

このプロジェクトをフォークして独自に公開する場合は、公開前にローカルツールの設定へ一度でも貼り付けた・コミットしたトークン類を必ずローテーションしてください。

## はじめかた

### 前提

- Python 3.12
- ダッシュボードを使う場合は Node.js 20 と npm
- 同梱のLaunchAgent自動化を再利用する場合のみmacOSが必要。バックエンドとフロントエンドの手動開発には不要
- 有効化する外部データ・AI機能に対応するAPIキー

サポート対象のPython環境には `torch`・`transformers`・ポートフォリオ最適化・リスク分析ライブラリが含まれるため、初回インストールは大きめです。

### 1. バックエンドとデモ状態

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# APIキーは ~/.almanac_secrets（シェル形式の KEY=VALUE、1行1項目）から
# 読み込まれます。プロジェクト直下の .env ファイルではありません — この
# リポジトリのどこもdotenvを読み込んでいません。
cp .env.example ~/.almanac_secrets
chmod 600 ~/.almanac_secrets

python scripts/init_private_state.py   # 欠けているローカル状態だけに
                                        # サンプル現金 + SPYを投入。
                                        # 既存状態は上書きしない

./start_v5.sh                      # FastAPIのみ127.0.0.1:8000で起動
```

`start_v5.sh` はバックエンドプロセスに接続したままになります。別のターミナルで次を実行して確認します。

```bash
curl http://127.0.0.1:8000/health
```

対話型APIドキュメントは <http://127.0.0.1:8000/docs> で確認できます。このスクリプトはFastAPIバックエンドだけを起動し、定期ジョブとダッシュボードは別のopt-inプロセスです。

### 2. ダッシュボード

```bash
cd frontend
npm ci
npm run dev                        # http://localhost:3000（上記のFastAPIバックエンドと通信）
```

read-only表示にはAPIキーは不要です。ローカルで書き込み操作を使う場合はキーを作成します。

```bash
mkdir -p ~/.config/almanac
python -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/almanac/api_key
chmod 600 ~/.config/almanac/api_key
```

同じ値を `frontend/.env.local` に設定します。

```dotenv
NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000
NEXT_PUBLIC_ALMANAC_API_KEY=<~/.config/almanac/api_key の内容>
```

`NEXT_PUBLIC_*` はブラウザへ配信されるJavaScriptに埋め込まれます。この設定は既定のlocalhost限定運用向けです。ダッシュボードやこのキーを信頼できないネットワークへ公開しないでください。

### 3. AI分析を実行する

```bash
./run_with_secrets.sh venv/bin/python portfolio_analyst.py --force
```

このコマンドは外部APIを実際に呼び、プロバイダ料金が発生する場合があります。既定の `ALMANAC_PRIVACY_MODE=strict_local` は、book-awareな最終合成を意図的にブロックします。`anthropic_book_aware` または `multi_provider_book_aware` は、各モードでどのポートフォリオ情報が外部へ出るかを確認してから有効にしてください。

### 4. ローカル検証

```bash
venv/bin/python -m pytest tests/ -q
python scripts/check_public_safety.py
git diff --check

cd frontend
npm ci
npm run lint
npm test
npm run build
```

## ディレクトリ構成

```
almanac/                 コアパッケージ — ランタイム設定・LLM安全層・DBマイグレーション・可観測性
analyst/                 LLM分析パイプライン（マルチモデル・ケース別）
api/                     FastAPIルート
frontend/                Next.jsダッシュボード
examples/private_state/  ローカル専用状態ファイルのテンプレート（コミットされない）
tests/                   pytestスイート
model_router.py           ロールベースのClaudeルーティングとbudget mode変換
policy_engine.py          順序付きの決定論的アクションゲート
event_ledger.py           append-only監査イベントと整合性検査
objective.md              バージョン管理された目的関数とハード制約
```

その他のトップレベルの `.py` ファイルの多くは、パッケージの一部というより単機能モジュール（スクリーナー、データ取得、ポリシー/リスクエンジン、税務ツール等）です。詳細は各ファイルのdocstringを参照してください。

## 免責事項

これは個人が自身のポートフォリオのために構築した個人プロジェクトです。投資助言ではなく、第三者による正確性の監査も受けていません。中身に興味のある方向けにそのまま公開しているものであり、利用は自己責任でお願いします。

## ライセンス

[MIT](LICENSE)
