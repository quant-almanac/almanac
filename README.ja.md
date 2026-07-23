# ALMANAC

*[English](README.md)*

**ALMANAC** は、個人のポートフォリオ運用を支援するAIアシスト型の資産管理・リスク管理システムです。Pythonバックエンドと Next.js ダッシュボードを組み合わせ、実際の長期投資口座に対して日次のポートフォリオ分析・銘柄スクリーニング・規律あるリスク管理を行います。AIの提案と実際の発注の間には、必ず決定論的なガードレールが挟まる設計です。

このリポジトリは、そのシステムの**公開用に匿名化したスナップショット**です。実運用データ・認証情報・保有者を特定しうる情報は意図的に除外しています（詳細は [Public Repository Safety](#public-repository-safety)）。

## できること

目的関数は明文化されバージョン管理されています（[`objective.md`](objective.md)）：**税引後・手数料控除後・円建ての時間加重収益率（TWR）を、グローバル株式60%／グローバル債券40%のベンチマークに対して最大化する**こと。これはVaR・ドローダウン・VIX連動のサーキットブレーカーといったハードな制約下にあり、これらはLLMの判断ではなく決定論的なポリシーエンジンが強制します。

| 領域 | 内容 |
|---|---|
| **ポートフォリオ・リスク** | LLM生成ビューを用いたBlack-Litterman最適化、GJR-GARCHによるボラティリティモデリング、相場レジーム判定（強気／中立／弱気／クラッシュ）、集中リスク・人的資本エクスポージャーの上限管理 |
| **AI判断支援** | Claude + DeepSeekによるマルチモデル分析（タスクごとにコスト最適なモデルを選択）。トリム・買い増し・リバランス・損出しといったケース別判断を、発注に至る前に必ず決定論的ポリシーでゲーティング |
| **スクリーニング・シグナル** | 日米のファンダメンタルズ長期スクリーニング、開示（EDINET／TDnet／EDGAR）起点のカタリスト検知、信用・空売り候補スクリーニング、インサイダークラスター・IPO監視 |
| **執行・ガードレール** | 日次／月次ドローダウンのサーキットブレーカー、VaR・VIX連動の発注ブロック、監査用のappend-onlyイベント台帳、既存注文を考慮したポジションサイジング |
| **税務・口座管理** | FIFO/LIFO/損出し/利益最小化の税ロット戦略、NISA枠の追跡、持株会（従業員株式制度）の集中度管理 |
| **可観測性** | ベンチマーク対比のNAV/TWR実績追跡（Modified Dietz法）。固定的な実績主張ではなく、実測値をそのまま示す検証ページ |

## アーキテクチャ

- **バックエンド** — Python 3.12 / FastAPI。ポートフォリオ最適化（[PyPortfolioOpt](https://github.com/robertmartin8/PyPortfolioOpt)、[riskfolio-lib](https://riskfolio-lib.readthedocs.io/)、[skfolio](https://skfolio.org/)）、GARCHリスクモデリング（[arch](https://arch.readthedocs.io/)）、FinBERTセンチメント分析（`transformers` / `torch`）、AI分析にClaude（Anthropic）とDeepSeekを使用。
- **フロントエンド** — Next.js 16（App Router）/ React 19 / TypeScript。ポートフォリオ・スクリーニング・リスク・シナリオ・戦略・信用取引・NISA・AI判断支援・執行ログ・パフォーマンス検証ページを1つのコンソールに統合。
- **プライバシー層** — 外部LLMへの呼び出しは全て、保有銘柄・残高等の帳簿情報を送信前に取り除くサニタイザ（`almanac/llm_safety.py`）を経由します。外部モデルが見るのは匿名化された市場コンテキストのみで、実際のポートフォリオを見ることはありません。

## 設定（Configuration）

`.env.example` を `.env` にコピーし、必要な項目だけ埋めてください。コードを読むだけなら何も設定は不要です。実際にシステムを動かす場合にのみ関係します。

**AI機能に必須**

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

## 公開リポジトリの安全性（Public Repository Safety）

このリポジトリは、ローカルのポートフォリオ状態・証券会社からのエクスポート・データベース・ログ・スクリーンショット・ローカルAIツールのセッション・APIキーを意図的に含んでいません。

`holdings.json`・`account.json`・`nisa_portfolio.json`・`trade_history.csv`・`almanac.db` などはGitで無視され、ローカル環境の外に出ることはありません。ドキュメント中の数値例は実際の金額ではなく、丸めたプレースホルダーを使用しています。`scripts/check_public_safety.py` は、既知の個人識別情報やシークレットキーのパターンをトラッキング対象ファイルから検出するスクリプトで、pushの前に実行する想定です。

このプロジェクトをフォークして独自に公開する場合は、公開前にローカルツールの設定へ一度でも貼り付けた・コミットしたトークン類を必ずローテーションしてください。

## はじめかた

### バックエンド

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env               # 自分のAPIキーを設定
python scripts/init_private_state.py   # examples/ から空のローカル状態ファイルを作成

./start_v5.sh                      # FastAPIが:8000、Next.jsダッシュボードが:3000で起動
```

### フロントエンドのみ

```bash
cd frontend
npm install
npm run dev                        # http://localhost:3000
```

書き込み系エンドポイントには `ALMANAC_API_KEY`（または `~/.config/almanac/api_key` のキーファイル）が必要です。

## ディレクトリ構成

```
almanac/                 コアパッケージ — ランタイム設定・LLM安全層・DBマイグレーション・可観測性
analyst/                 LLM分析パイプライン（マルチモデル・ケース別）
api/                     FastAPIルート
frontend/                Next.jsダッシュボード
examples/private_state/  ローカル専用状態ファイルのテンプレート（コミットされない）
tests/                   pytestスイート
```

その他のトップレベルの `.py` ファイルの多くは、パッケージの一部というより単機能モジュール（スクリーナー、データ取得、ポリシー/リスクエンジン、税務ツール等）です。詳細は各ファイルのdocstringを参照してください。

## 免責事項

これは個人が自身のポートフォリオのために構築した個人プロジェクトです。投資助言ではなく、第三者による正確性の監査も受けていません。中身に興味のある方向けにそのまま公開しているものであり、利用は自己責任でお願いします。
