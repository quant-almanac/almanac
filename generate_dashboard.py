import json, csv, os
import yfinance as yf
from datetime import datetime

def generate():
    # 保有銘柄
    holdings_path = os.path.expanduser('~/portfolio-bot/holdings.json')
    holdings = {}
    if os.path.exists(holdings_path):
        with open(holdings_path) as f:
            holdings = json.load(f)

    positions = []
    total_pnl_amt = 0
    for ticker, info in holdings.items():
        try:
            stock = yf.Ticker(ticker)
            price = stock.fast_info['lastPrice']
            hist = stock.history(period='60d')
            entry = info['entry_price']
            shares = info['shares']
            pnl_pct = (price - entry) / entry * 100
            pnl_amt = (price - entry) * shares
            total_pnl_amt += pnl_amt
            chart_dates = [d.strftime('%Y-%m-%d') for d in hist.index[-40:]]
            chart_prices = [round(float(v), 2) for v in hist['Close'].iloc[-40:]]
            positions.append({
                'ticker': ticker,
                'entry': entry,
                'current': round(price, 2),
                'shares': shares,
                'pnl_pct': round(pnl_pct, 2),
                'pnl_amt': round(pnl_amt, 2),
                'date': info.get('entry_date', '-'),
                'reason': info.get('reason', ''),
                'target': info.get('target_price', '-'),
                'stop_loss': info.get('stop_loss', '-'),
                'holding_period': info.get('holding_period', '-'),
                'score': info.get('score', '-'),
                'chart_dates': chart_dates,
                'chart_prices': chart_prices,
            })
        except:
            pass

    # 売買履歴
    trades = []
    history_path = os.path.expanduser('~/portfolio-bot/trade_history.csv')
    if os.path.exists(history_path):
        with open(history_path, encoding='utf-8') as f:
            trades = list(csv.DictReader(f))
        trades.reverse()

    sells = [t for t in trades if t['アクション'] == 'SELL']
    wins = sum(1 for t in sells if t['損益%'] and float(t['損益%'].replace('%','')) > 0)
    win_rate = int(wins / len(sells) * 100) if sells else 0

    # スクリーニング結果
    screen_results = []
    screen_time = '-'
    screen_meta_text = ''
    screen_strategy_counts = {}
    screen_path = os.path.expanduser('~/portfolio-bot/screen_results.json')
    if os.path.exists(screen_path):
        with open(screen_path) as f:
            sr = json.load(f)
        screen_results = sr.get('candidates', [])[:10]
        screen_time = sr.get('timestamp', '-')
        screen_meta_text = sr.get('meta_text', '')
        screen_strategy_counts = sr.get('strategy_counts', {})

    # セクター強度
    sector_strength = {}
    sector_path = os.path.expanduser('~/portfolio-bot/sector_strength.json')
    if os.path.exists(sector_path):
        with open(sector_path) as f:
            sector_strength = json.load(f)

    # バックテスト結果
    backtest_results = {}
    bt_path = os.path.expanduser('~/portfolio-bot/backtest_full_results.json')
    if os.path.exists(bt_path):
        with open(bt_path) as f:
            backtest_results = json.load(f)
    wfo_results = {}
    wfo_path = os.path.expanduser('~/portfolio-bot/backtest_wfo_results.json')
    if os.path.exists(wfo_path):
        with open(wfo_path) as f:
            wfo_results = json.load(f)

    # 地合い情報
    try:
        spy_hist2 = yf.Ticker('SPY').history(period='3mo')
        spy_price2 = float(spy_hist2['Close'].iloc[-1])
        spy_ma50 = float(spy_hist2['Close'].rolling(50).mean().iloc[-1])
        spy_above = spy_price2 > spy_ma50
        nk_hist2 = yf.Ticker('^N225').history(period='3mo')
        nk_price2 = float(nk_hist2['Close'].iloc[-1])
        nk_ma50 = float(nk_hist2['Close'].rolling(50).mean().iloc[-1])
        nk_above = nk_price2 > nk_ma50
        spy_ma50_disp = round(spy_ma50, 2)
        nk_ma50_disp = round(nk_ma50, 0)
    except:
        spy_above, nk_above = True, True
        spy_ma50_disp, nk_ma50_disp = '-', '-' 

    # 最終分析
    log_path = os.path.expanduser('~/portfolio-bot/log.txt')
    last_run = 'なし'
    if os.path.exists(log_path):
        with open(log_path) as f:
            lines = f.readlines()
        for line in reversed(lines):
            if '分析完了' in line:
                last_run = line.strip()
                break

    # マクロデータ
    try:
        vix = round(yf.Ticker('^VIX').fast_info['lastPrice'], 1)
        usdjpy = round(yf.Ticker('JPY=X').fast_info['lastPrice'], 1)
        tnx = round(yf.Ticker('^TNX').fast_info['lastPrice'], 2)
        spy_hist = yf.Ticker('SPY').history(period='5d')
        spy_chg = round((spy_hist['Close'].iloc[-1] - spy_hist['Close'].iloc[-2]) / spy_hist['Close'].iloc[-2] * 100, 2)
        spy_price = round(float(spy_hist['Close'].iloc[-1]), 2)
    except:
        vix, usdjpy, tnx, spy_chg, spy_price = '-', '-', '-', '-', '-'

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    total_color = '#00ff88' if total_pnl_amt >= 0 else '#ff4444'

    # チャートJS
    charts_js = ''
    for p in positions:
        pnl_color = '#00ff88' if p['pnl_pct'] >= 0 else '#ff4444'
        # エントリー日に最も近いインデックスを探す
        entry_idx = None
        min_diff = 9999
        for i, d in enumerate(p['chart_dates']):
            diff = abs((len(p['chart_dates']) - 1 - i))
            # 日付文字列で比較
            if d <= p['date']:
                entry_idx = i
        # entry_idxが見つからない場合は最初のインデックスを使用
        if entry_idx is None and p['chart_dates']:
            entry_idx = 0

        charts_js += f"""
        new Chart(document.getElementById('chart_{p["ticker"]}'), {{
            type: 'line',
            data: {{
                labels: {json.dumps(p['chart_dates'])},
                datasets: [
                    {{
                        data: {json.dumps(p['chart_prices'])},
                        borderColor: '{pnl_color}',
                        borderWidth: 2,
                        pointRadius: 0,
                        fill: true,
                        backgroundColor: '{pnl_color}15',
                        tension: 0.3
                    }},
                    {{
                        data: Array({len(p['chart_prices'])}).fill(null).map((_, i) => i === {entry_idx if entry_idx is not None else 'null'} ? {p['entry']} : null),
                        pointRadius: Array({len(p['chart_prices'])}).fill(0).map((_, i) => i === {entry_idx if entry_idx is not None else 'null'} ? 6 : 0),
                        pointBackgroundColor: '#ffdd00',
                        pointBorderColor: '#ffdd00',
                        borderWidth: 0,
                        showLine: false,
                        fill: false,
                    }},
                    {{
                        data: Array({len(p['chart_prices'])}).fill({p['target'] if isinstance(p['target'], (int,float)) else 'null'}),
                        borderColor: '#00ff8840',
                        borderWidth: 1,
                        borderDash: [4,4],
                        pointRadius: 0,
                        fill: false,
                        tension: 0
                    }},
                    {{
                        data: Array({len(p['chart_prices'])}).fill({p['stop_loss'] if isinstance(p['stop_loss'], (int,float)) else 'null'}),
                        borderColor: '#ff444440',
                        borderWidth: 1,
                        borderDash: [4,4],
                        pointRadius: 0,
                        fill: false,
                        tension: 0
                    }}
                ]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{ display: false }},
                    tooltip: {{
                        callbacks: {{
                            title: ctx => ctx[0].label,
                            label: ctx => ctx.dataset.data[ctx.dataIndex] ? '$' + ctx.dataset.data[ctx.dataIndex] : ''
                        }}
                    }}
                }},
                scales: {{
                    x: {{ display: false }},
                    y: {{
                        display: true,
                        grid: {{ color: '#1a2535' }},
                        ticks: {{ color: '#4a6080', font: {{ size: 10 }}, maxTicksLimit: 4,
                            callback: v => '$' + v }}
                    }}
                }}
            }}
        }});"""

    # ポジションカード
    position_cards = ''
    for p in positions:
        pnl_color = '#00ff88' if p['pnl_pct'] >= 0 else '#ff4444'
        pnl_icon = '▲' if p['pnl_pct'] >= 0 else '▼'
        target = p['target']
        stop = p['stop_loss']
        curr = p['current']
        target_pct = round((float(target) - curr) / curr * 100, 1) if isinstance(target, (int,float)) else '-'
        stop_pct = round((curr - float(stop)) / curr * 100, 1) if isinstance(stop, (int,float)) else '-'
        rr = round(abs(target_pct / stop_pct), 1) if isinstance(target_pct, float) and isinstance(stop_pct, float) and stop_pct != 0 else '-'
        stars = '★' * int(p['score']) + '☆' * (5 - int(p['score'])) if str(p['score']).isdigit() else ''

        position_cards += f"""
        <div class="position-card">
            <div class="pos-header">
                <div>
                    <div class="pos-ticker">{p['ticker']}</div>
                    <div class="pos-date">取得: {p['date']} | {p['holding_period']}</div>
                </div>
                <div style="text-align:right">
                    <div class="pos-pnl" style="color:{pnl_color}">{pnl_icon} {p['pnl_pct']:+.2f}%</div>
                    <div class="pos-pnl-amt" style="color:{pnl_color}">${p['pnl_amt']:+,.0f}</div>
                </div>
            </div>
            <div class="pos-chart-wrap">
                <canvas id="chart_{p['ticker']}"></canvas>
                <div class="chart-legend">
                    <span style="color:#ffdd00">● エントリー</span>
                    <span style="color:#00ff8880">— 目標</span>
                    <span style="color:#ff444480">— 損切り</span>
                </div>
            </div>
            <div class="pos-metrics">
                <div class="metric">
                    <div class="metric-label">現在値</div>
                    <div class="metric-value">${curr}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">目標 (+{target_pct}%)</div>
                    <div class="metric-value" style="color:#00ff88">${target}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">損切り (-{stop_pct}%)</div>
                    <div class="metric-value" style="color:#ff4444">${stop}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">リスクリワード</div>
                    <div class="metric-value" style="color:#0088ff">1:{rr}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">株数</div>
                    <div class="metric-value">{p['shares']}株</div>
                </div>
                <div class="metric">
                    <div class="metric-label">信頼度</div>
                    <div class="metric-value" style="color:#ffa500;font-size:11px">{stars}</div>
                </div>
            </div>
            {"<div class='pos-reason'><div class='reason-label'>💡 AI判断理由</div><p>" + p['reason'] + "</p></div>" if p['reason'] else ""}
        </div>"""

    # 売買履歴
    trade_rows = ''
    for t in trades[:20]:
        ac = 'buy' if t['アクション'] == 'BUY' else 'sell'
        pnl = t['損益%']
        pc = ''
        if pnl:
            pc = 'color:#00ff88' if float(pnl.replace('%','')) > 0 else 'color:#ff4444'
        trade_rows += f"<tr><td class='td-time'>{t['日時']}</td><td><span class='badge {ac}'>{t['アクション']}</span></td><td class='td-ticker'>{t['ティッカー']}</td><td>${t['価格']}</td><td>{t['株数']}株</td><td style='{pc}'>{pnl}</td><td style='{pc}'>{t['損益額']}</td></tr>"

    # スクリーニング（5戦略対応）
    screen_rows = ''
    strategy_colors = {
        '逆張り': '#ff8c00',
        'モメンタム': '#00ff88',
        'ギャップダウン': '#ff4444',
        'イベントドリブン後': '#cc44ff',
        'イベントドリブン前': '#0088ff',
    }
    for s in screen_results:
        rsi = s.get('rsi', '-')
        rc = '#ff4444' if isinstance(rsi,(int,float)) and rsi < 20 else '#ffa500' if isinstance(rsi,(int,float)) and rsi < 35 else '#00ff88'
        strategy = s.get('strategy', '-')
        sc = strategy_colors.get(strategy, '#4a6080')
        atr = s.get('atr_pct', '-')
        stop = s.get('stop_loss_atr', '-')
        score = s.get('score', '-')
        market = '🇯🇵' if s.get('is_japan') else '🇺🇸'
        screen_rows += f"""<tr>
            <td class='td-ticker'>{market} {s.get('ticker','-')}</td>
            <td><span style='background:{sc}20;color:{sc};padding:2px 8px;border-radius:3px;font-size:11px'>{strategy}</span></td>
            <td style='color:{rc}'>{rsi}</td>
            <td>{s.get('mom_5d',s.get('mom_1m','-'))}%</td>
            <td>{s.get('volume_ratio','-')}x</td>
            <td style='color:#4a6080;font-size:11px'>{atr}%</td>
            <td style='color:#ff4444;font-size:11px'>${stop}</td>
            <td style='color:#ffa500'>{score}</td>
            <td style='font-size:11px;color:#8aa0b8;max-width:200px'>{s.get('reason','-')}</td>
        </tr>"""

    # セクター強度テーブル
    sector_rows = ''
    for sector, v in list(sector_strength.items())[:8]:
        sc = '#00ff88' if v.get('strong') else '#ff4444'
        mark = '▲' if v.get('strong') else '▼'
        sector_rows += f"<tr><td style='color:{sc}'>{mark} {sector}</td><td>{v.get('etf','-')}</td><td style='color:{sc}'>{v.get('mom_1m','-'):+.1f}%</td><td style='color:{sc}'>{v.get('mom_3m','-'):+.1f}%</td><td style='color:{sc};font-weight:700'>{v.get('rel_1m','-'):+.1f}%</td><td style='color:{sc}'>{v.get('score','-'):+.2f}</td></tr>" if isinstance(v.get('mom_1m'), (int,float)) else ""

    # バックテスト結果テーブル
    bt_rows = ''
    bt_ranking = []
    for label, r in backtest_results.items():
        if r.get('stats'):
            bt_ranking.append((r['stats']['profit_factor'], label, r['stats']))
    bt_ranking.sort(reverse=True)
    for pf, label, stats in bt_ranking:
        pf_color = '#00ff88' if pf >= 2.0 else '#ffa500' if pf >= 1.5 else '#ff4444'
        bt_rows += f"<tr><td class='td-ticker'>{label.replace('_',' ')}</td><td style='color:{pf_color};font-weight:700'>{pf}</td><td style='color:{'#00ff88' if stats['win_rate']>=55 else '#ffa500'}'>{stats['win_rate']}%</td><td style='color:#00ff88'>{stats['avg_pnl']:+.2f}%</td><td style='color:var(--dim)'>{stats['trades']}</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="300">
<title>ALMANAC Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Noto+Sans+JP:wght@300;400;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#0e1419; --surface:#141e2a; --surface2:#0f1720;
  --border:#1a2535; --accent:#00ff88; --accent2:#0088ff;
  --danger:#ff4444; --warn:#ffa500;
  --text:#c8d8e8; --dim:#4a6080;
  --mono:'Space Mono',monospace; --sans:'Noto Sans JP',sans-serif;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:var(--sans);font-weight:300;
  background-image:radial-gradient(ellipse at 15% 50%,#00ff8808 0%,transparent 55%),
    radial-gradient(ellipse at 85% 20%,#0088ff08 0%,transparent 55%);}}
a{{color:var(--accent2);text-decoration:none}}
a:hover{{text-decoration:underline}}

/* ヘッダー */
.header{{display:flex;align-items:center;justify-content:space-between;
  padding:16px 28px;border-bottom:1px solid var(--border);background:var(--surface);
  position:sticky;top:0;z-index:100}}
.logo{{font-family:var(--mono);font-size:16px;color:var(--accent);letter-spacing:3px}}
.logo span{{color:var(--dim)}}
.nav{{display:flex;gap:6px}}
.nav a{{font-family:var(--mono);font-size:11px;color:var(--dim);
  padding:6px 14px;border:1px solid var(--border);border-radius:4px;
  letter-spacing:1px;transition:all .2s}}
.nav a:hover,.nav a.active{{color:var(--accent);border-color:var(--accent);
  background:#00ff8810;text-decoration:none}}
.header-right{{font-family:var(--mono);font-size:10px;color:var(--dim);text-align:right;line-height:1.8}}
.dot{{display:inline-block;width:6px;height:6px;border-radius:50%;
  background:var(--accent);margin-right:5px;animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}

/* ページ */
.page{{display:none;padding:24px 28px;max-width:1600px;margin:0 auto}}
.page.active{{display:block}}

/* サマリー */
.summary-grid{{display:grid;grid-template-columns:repeat(6,1fr);gap:14px;margin-bottom:24px}}
.scard{{background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:16px;position:relative;overflow:hidden}}
.scard::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,var(--accent),transparent)}}
.scard.blue::before{{background:linear-gradient(90deg,var(--accent2),transparent)}}
.scard.warn::before{{background:linear-gradient(90deg,var(--warn),transparent)}}
.slabel{{font-size:10px;color:var(--dim);letter-spacing:1px;text-transform:uppercase;
  font-family:var(--mono);margin-bottom:6px}}
.svalue{{font-size:22px;font-family:var(--mono);font-weight:700;line-height:1}}
.ssub{{font-size:10px;color:var(--dim);margin-top:4px;font-family:var(--mono)}}

/* セクション */
.section{{margin-bottom:24px}}
.section-title{{font-family:var(--mono);font-size:10px;color:var(--dim);
  letter-spacing:2px;text-transform:uppercase;margin-bottom:12px;
  display:flex;align-items:center;gap:10px}}
.section-title::after{{content:'';flex:1;height:1px;background:var(--border)}}

/* ポジションカード */
.positions-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:16px}}
.position-card{{background:var(--surface);border:1px solid var(--border);
  border-radius:8px;overflow:hidden;transition:border-color .2s}}
.position-card:hover{{border-color:#2a3a50}}
.pos-header{{display:flex;justify-content:space-between;align-items:flex-start;padding:16px 18px 12px}}
.pos-ticker{{font-family:var(--mono);font-size:22px;font-weight:700;color:var(--accent)}}
.pos-date{{font-size:11px;color:var(--dim);font-family:var(--mono);margin-top:3px}}
.pos-pnl{{font-family:var(--mono);font-size:20px;font-weight:700;text-align:right}}
.pos-pnl-amt{{font-family:var(--mono);font-size:13px;text-align:right;margin-top:2px}}
.pos-chart-wrap{{height:120px;padding:0 14px 4px;position:relative}}
.chart-legend{{display:flex;gap:12px;padding:4px 0 8px;font-family:var(--mono);font-size:10px;color:var(--dim)}}
.pos-metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:0;
  border-top:1px solid var(--border)}}
.metric{{padding:10px 16px;border-right:1px solid var(--border);border-bottom:1px solid var(--border)}}
.metric:nth-child(3n){{border-right:none}}
.metric:nth-child(4),.metric:nth-child(5),.metric:nth-child(6){{border-bottom:none}}
.metric-label{{font-size:10px;color:var(--dim);font-family:var(--mono);margin-bottom:3px}}
.metric-value{{font-size:14px;font-family:var(--mono);font-weight:700}}
.pos-reason{{padding:12px 18px;border-top:1px solid var(--border);background:var(--surface2)}}
.reason-label{{font-size:10px;color:var(--dim);letter-spacing:1px;text-transform:uppercase;
  font-family:var(--mono);display:block;margin-bottom:6px}}
.pos-reason p{{font-size:12px;line-height:1.8;color:#8aa0b8}}

/* テーブル */
.table-wrap{{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden}}
table{{width:100%;border-collapse:collapse}}
th{{background:var(--surface2);color:var(--dim);font-family:var(--mono);font-size:10px;
  letter-spacing:1px;text-transform:uppercase;padding:10px 16px;text-align:left;
  border-bottom:1px solid var(--border)}}
td{{padding:9px 16px;font-size:13px;border-bottom:1px solid #0d1520;font-family:var(--mono)}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#0f1825}}
.td-ticker{{color:var(--accent);font-weight:700}}
.td-time{{color:var(--dim);font-size:11px}}
.badge{{padding:2px 8px;border-radius:3px;font-size:11px;font-weight:700;letter-spacing:1px}}
.badge.buy{{background:#00ff8820;color:#00ff88}}
.badge.sell{{background:#ff444420;color:#ff4444}}
.reason-tag{{background:#0088ff15;color:var(--accent2);padding:2px 8px;border-radius:3px;font-size:11px}}
.no-data{{text-align:center;padding:40px;color:var(--dim);font-family:var(--mono);font-size:13px}}

/* マクロパネル */
.macro-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px}}
.macro-card{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px}}
.macro-name{{font-family:var(--mono);font-size:10px;color:var(--dim);letter-spacing:1px;margin-bottom:8px}}
.macro-value{{font-family:var(--mono);font-size:28px;font-weight:700}}
.macro-sub{{font-size:11px;color:var(--dim);margin-top:4px}}

/* システム設計ページ */
.design-page{{max-width:900px}}
.design-section{{background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:28px;margin-bottom:20px}}
.design-section h3{{font-family:var(--mono);color:var(--accent);font-size:14px;
  letter-spacing:2px;margin-bottom:16px;text-transform:uppercase}}
.design-section p{{font-size:14px;line-height:2;color:#8aa0b8;margin-bottom:12px}}
.design-section p:last-child{{margin-bottom:0}}
.design-flow{{display:flex;flex-direction:column;gap:0}}
.flow-step{{display:flex;align-items:flex-start;gap:16px;padding:12px 0;
  border-bottom:1px solid var(--border)}}
.flow-step:last-child{{border-bottom:none}}
.flow-num{{font-family:var(--mono);font-size:11px;color:var(--accent);
  background:#00ff8815;border:1px solid #00ff8830;border-radius:4px;
  padding:4px 10px;white-space:nowrap;margin-top:2px}}
.flow-content{{flex:1}}
.flow-title{{font-family:var(--mono);font-size:13px;color:var(--text);margin-bottom:4px}}
.flow-desc{{font-size:12px;color:var(--dim);line-height:1.8}}
.agent-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:16px}}
.agent-card{{background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:14px}}
.agent-name{{font-family:var(--mono);font-size:11px;margin-bottom:6px}}
.agent-desc{{font-size:11px;color:var(--dim);line-height:1.7}}
.risk-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:16px}}
.risk-item{{background:var(--surface2);border-left:3px solid var(--danger);
  border-radius:0 6px 6px 0;padding:12px}}
.risk-item.green{{border-color:var(--accent)}}
.risk-item.blue{{border-color:var(--accent2)}}
.risk-label{{font-family:var(--mono);font-size:11px;color:var(--dim);margin-bottom:4px}}
.risk-value{{font-family:var(--mono);font-size:14px;font-weight:700}}

.footer{{text-align:center;padding:16px;font-family:var(--mono);font-size:10px;
  color:var(--dim);border-top:1px solid var(--border)}}
</style>
</head>
<body>

<div class="header">
  <div class="logo">Nexus<span>Trader</span></div>
  <div class="nav">
    <a href="#" class="active" onclick="showPage('dashboard',this)">DASHBOARD</a>
    <a href="#" onclick="showPage('macro',this)">MACRO</a>
    <a href="#" onclick="showPage('screening',this)">SCREENING</a>
    <a href="#" onclick="showPage('history',this)">HISTORY</a>
    <a href="#" onclick="showPage('backtest',this)">BACKTEST</a>
    <a href="#" onclick="showPage('design',this)">SYSTEM DESIGN</a>
  </div>
  <div class="header-right">
    <span class="dot"></span>SYSTEM ACTIVE<br>
    {last_run}<br>
    更新: {now}
  </div>
</div>

<!-- DASHBOARDページ -->
<div id="page-dashboard" class="page active">
  <div class="summary-grid">
    <div class="scard">
      <div class="slabel">保有銘柄</div>
      <div class="svalue">{len(positions)}</div>
      <div class="ssub">アクティブポジション</div>
    </div>
    <div class="scard">
      <div class="slabel">含み損益</div>
      <div class="svalue" style="color:{total_color}">${total_pnl_amt:+,.0f}</div>
      <div class="ssub">未実現損益合計</div>
    </div>
    <div class="scard">
      <div class="slabel">通算勝率</div>
      <div class="svalue" style="color:{'var(--accent)' if win_rate >= 50 else 'var(--danger)'}">{win_rate}%</div>
      <div class="ssub">{wins}勝 {len(sells)-wins}敗</div>
    </div>
    <div class="scard blue">
      <div class="slabel">VIX</div>
      <div class="svalue" style="color:{'var(--danger)' if isinstance(vix,float) and vix>25 else 'var(--warn)' if isinstance(vix,float) and vix>20 else 'var(--accent)'}">{vix}</div>
      <div class="ssub">{'⚠ 高ボラ' if isinstance(vix,float) and vix>25 else '普通'}</div>
    </div>
    <div class="scard blue">
      <div class="slabel">USD/JPY</div>
      <div class="svalue">{usdjpy}</div>
      <div class="ssub">米10年債: {tnx}%</div>
    </div>
    <div class="scard blue">
      <div class="slabel">SPY</div>
      <div class="svalue" style="color:{'var(--accent)' if isinstance(spy_chg,float) and spy_chg>=0 else 'var(--danger)'}">${spy_price}</div>
      <div class="ssub" style="color:{'var(--accent)' if isinstance(spy_chg,float) and spy_chg>=0 else 'var(--danger)'}">{spy_chg:+.2f}%</div>
    </div>
  </div>

  <!-- 地合いバナー -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:24px">
    <div class="scard" style="border-color:{'#00ff8840' if spy_above else '#ff444440'}">
      <div class="slabel">S&P500 vs 50日線</div>
      <div class="svalue" style="font-size:16px;color:{'var(--accent)' if spy_above else 'var(--danger)'}">{'50日線の上 ▲' if spy_above else '50日線の下 ▼'}</div>
      <div class="ssub">MA50: ${spy_ma50_disp} | {'強気トレンド維持' if spy_above else '弱気：逆張り信頼性低下'}</div>
    </div>
    <div class="scard" style="border-color:{'#00ff8840' if nk_above else '#ff444440'}">
      <div class="slabel">日経225 vs 50日線</div>
      <div class="svalue" style="font-size:16px;color:{'var(--accent)' if nk_above else 'var(--danger)'}">{'50日線の上 ▲' if nk_above else '50日線の下 ▼'}</div>
      <div class="ssub">MA50: ¥{nk_ma50_disp} | {'強気トレンド維持' if nk_above else '弱気：逆張り信頼性低下'}</div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">保有ポジション</div>
    {"<div class='positions-grid'>" + position_cards + "</div>" if positions else "<div class='no-data'>現在保有中の銘柄はありません</div>"}
  </div>
</div>

<!-- MACROページ -->
<div id="page-macro" class="page">
  <div class="macro-grid">
    <div class="macro-card">
      <div class="macro-name">VIX — 恐怖指数</div>
      <div class="macro-value" style="color:{'#ff4444' if isinstance(vix,float) and vix>30 else '#ffa500' if isinstance(vix,float) and vix>20 else '#00ff88'}">{vix}</div>
      <div class="macro-sub">{'🔴 サーキットブレーカー発動レベル' if isinstance(vix,float) and vix>30 else '🟡 やや不安定' if isinstance(vix,float) and vix>20 else '🟢 安定'}</div>
    </div>
    <div class="macro-card">
      <div class="macro-name">USD/JPY</div>
      <div class="macro-value">{usdjpy}</div>
      <div class="macro-sub">円建て換算に影響</div>
    </div>
    <div class="macro-card">
      <div class="macro-name">米10年債利回り</div>
      <div class="macro-value">{tnx}%</div>
      <div class="macro-sub">{'高金利環境 → グロース株に逆風' if isinstance(tnx,float) and tnx>4.0 else '中立'}</div>
    </div>
    <div class="macro-card">
      <div class="macro-name">SPY (S&P500 ETF)</div>
      <div class="macro-value" style="color:{'#00ff88' if isinstance(spy_chg,float) and spy_chg>=0 else '#ff4444'}">${spy_price}</div>
      <div class="macro-sub" style="color:{'#00ff88' if isinstance(spy_chg,float) and spy_chg>=0 else '#ff4444'}">{spy_chg:+.2f}% (前日比)</div>
    </div>
  </div>
  <div class="section">
    <div class="section-title">マクロ判断基準</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>指標</th><th>現在値</th><th>閾値</th><th>判断</th></tr></thead>
        <tbody>
          <tr><td>VIX</td><td style="color:{'#ff4444' if isinstance(vix,float) and vix>30 else '#00ff88'}">{vix}</td><td>&gt;30 = 全停止</td><td>{'🔴 売買停止' if isinstance(vix,float) and vix>30 else '🟢 通常運転'}</td></tr>
          <tr><td>USD/JPY</td><td>{usdjpy}</td><td>参考値</td><td>円建てコストに影響</td></tr>
          <tr><td>米10年債</td><td>{tnx}%</td><td>&gt;4.5% = 注意</td><td>{'🟡 グロース株注意' if isinstance(tnx,float) and tnx>4.5 else '🟢 許容範囲'}</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<!-- SCREENINGページ -->
<div id="page-screening" class="page">

  <!-- 戦略別カウント -->
  <div style="display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap">
    <div style="font-family:var(--mono);font-size:11px;color:var(--dim);padding:6px 14px;border:1px solid var(--border);border-radius:4px">
      地合い: {screen_meta_text}
    </div>
    {"".join([f'<div style="font-family:var(--mono);font-size:11px;padding:6px 14px;border-radius:4px;background:{strategy_colors.get(k,"#333")}20;color:{strategy_colors.get(k,"#888")};border:1px solid {strategy_colors.get(k,"#333")}40">{k}: {v}件</div>' for k,v in screen_strategy_counts.items() if v > 0])}
  </div>

  <div class="section">
    <div class="section-title">分析候補銘柄 — {screen_time}</div>
    <div class="table-wrap">
      {"<table><thead><tr><th>銘柄</th><th>戦略</th><th>RSI</th><th>5日騰落</th><th>出来高比</th><th>ATR%</th><th>ストップ目安</th><th>スコア</th><th>シグナル理由</th></tr></thead><tbody>" + screen_rows + "</tbody></table>" if screen_results else "<div class='no-data'>スクリーニングデータなし<br>analyzer.pyを実行してください</div>"}
    </div>
  </div>

  <!-- セクター強度 -->
  <div class="section">
    <div class="section-title">セクター強度（SPY比 相対モメンタム）</div>
    <div class="table-wrap">
      {"<table><thead><tr><th>セクター</th><th>ETF</th><th>1M騰落</th><th>3M騰落</th><th>相対1M</th><th>総合スコア</th></tr></thead><tbody>" + sector_rows + "</tbody></table>" if sector_rows else "<div class='no-data'>セクターデータなし<br>analyzer.pyを実行すると自動更新されます</div>"}
    </div>
  </div>
</div>

<!-- HISTORYページ -->
<div id="page-history" class="page">
  <div class="summary-grid" style="grid-template-columns:repeat(4,1fr)">
    <div class="scard">
      <div class="slabel">総売買回数</div>
      <div class="svalue">{len(trades)}</div>
    </div>
    <div class="scard">
      <div class="slabel">クローズ回数</div>
      <div class="svalue">{len(sells)}</div>
    </div>
    <div class="scard">
      <div class="slabel">勝率</div>
      <div class="svalue" style="color:{'var(--accent)' if win_rate>=50 else 'var(--danger)'}">{win_rate}%</div>
    </div>
    <div class="scard">
      <div class="slabel">勝敗</div>
      <div class="svalue">{wins}勝{len(sells)-wins}敗</div>
    </div>
  </div>
  <div class="section">
    <div class="section-title">売買履歴（直近20件）</div>
    <div class="table-wrap">
      {"<table><thead><tr><th>日時</th><th>売買</th><th>銘柄</th><th>価格</th><th>株数</th><th>損益%</th><th>損益額</th></tr></thead><tbody>" + trade_rows + "</tbody></table>" if trades else "<div class='no-data'>売買履歴なし</div>"}
    </div>
  </div>
</div>

<!-- SYSTEM DESIGNページ -->
<!-- BACKTESTページ -->
<div id="page-backtest" class="page">

  <div class="section">
    <div class="section-title">バックテスト結果（全183銘柄・フルグリッドサーチ）</div>
    <div class="table-wrap">
      <table><thead><tr><th>戦略</th><th>PF</th><th>勝率</th><th>平均損益</th><th>件数</th></tr></thead><tbody>{bt_rows}</tbody></table>
    </div>
  </div>

  <div class="section">
    <div class="section-title">戦略有効性サマリー</div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px">
      <div class="scard" style="border-color:#00ff8840">
        <div class="slabel">最優秀（日本株）</div>
        <div class="svalue" style="font-size:14px;color:var(--accent)">逆張り(JP)</div>
        <div class="ssub">PF 7.15 | 勝率67.5%</div>
      </div>
      <div class="scard" style="border-color:#cc44ff40">
        <div class="slabel">高期待値（日本株）</div>
        <div class="svalue" style="font-size:14px;color:#cc44ff">イベント後(JP)</div>
        <div class="ssub">PF 8.17 | 平均+10.89%</div>
      </div>
      <div class="scard" style="border-color:#ffa50040">
        <div class="slabel">安定稼働</div>
        <div class="svalue" style="font-size:14px;color:var(--warn)">ギャップダウン(JP)</div>
        <div class="ssub">PF 3.40 | 件数多い</div>
      </div>
      <div class="scard" style="border-color:#ff444440">
        <div class="slabel">要注意</div>
        <div class="svalue" style="font-size:14px;color:var(--danger)">モメンタム(JP)</div>
        <div class="ssub">PF 1.69 | ほぼトントン</div>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">バックテスト上の注意点</div>
    <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:12px">
      <div class="risk-item"><div class="risk-label">⚠ サバイバーシップバイアス</div><div style="font-size:11px;color:var(--dim);margin-top:4px;line-height:1.7">現在上場中の銘柄のみ使用。実際の勝率は3-5%低い可能性がある。</div></div>
      <div class="risk-item"><div class="risk-label">⚠ 取引コスト未考慮</div><div style="font-size:11px;color:var(--dim);margin-top:4px;line-height:1.7">手数料・スプレッドを含めると平均損益は約0.3-0.5%低下する。</div></div>
      <div class="risk-item"><div class="risk-label">⚠ 上昇相場バイアス</div><div style="font-size:11px;color:var(--dim);margin-top:4px;line-height:1.7">2022-2025年は長期上昇トレンド。弱気相場での有効性は別途検証が必要。</div></div>
      <div class="risk-item green"><div class="risk-label">✅ エグジット戦略込み</div><div style="font-size:11px;color:var(--dim);margin-top:4px;line-height:1.7">2×ATRストップ＋トレーリング＋タイムストップを含めた実運用に近い条件で検証済み。</div></div>
    </div>
  </div>
</div>

<div id="page-design" class="page design-page">

  <div class="design-section">
    <h3>システム概要</h3>
    <p>ALMANACは、Claude APIのマルチエージェント分析と5戦略スクリーニングを組み合わせた自動株式分析システムです。183銘柄（米国135・日本株48）を対象に逆張り・モメンタム・ギャップダウン・イベントドリブン前後の5戦略でスクリーニングし、地合い・セクターローテーション・決算シーズンを考慮した上でAIが買いシグナルを検出した際にTelegram経由で通知します。</p>
    <p>設計思想の核心は「データに基づく判断・AIは分析・人間が最終決定」という原則です。全戦略はウォークフォワード最適化とレジーム別パラメータチューニングにより、過去データへの過剰適合を防いだ実運用に近い検証を経ています。</p>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:16px">
      <div class="risk-item green"><div class="risk-label">監視銘柄</div><div class="risk-value">183銘柄</div><div style="font-size:11px;color:var(--dim);margin-top:4px">米国株135 / 日本株48</div></div>
      <div class="risk-item green"><div class="risk-label">スクリーニング戦略</div><div class="risk-value">5戦略</div><div style="font-size:11px;color:var(--dim);margin-top:4px">優先度付き候補選出</div></div>
      <div class="risk-item blue"><div class="risk-label">月間運用コスト</div><div class="risk-value">¥800-1,300</div><div style="font-size:11px;color:var(--dim);margin-top:4px">API + 電力のみ</div></div>
    </div>
  </div>

  <div class="design-section">
    <h3>分析フロー</h3>
    <div class="design-flow">
      <div class="flow-step">
        <span class="flow-num">STEP 1</span>
        <div class="flow-content">
          <div class="flow-title">マクロ環境チェック</div>
          <div class="flow-desc">VIX・ドル円・米10年債利回りを取得し、マクロスコアを算出。VIX &gt; 30の場合はサーキットブレーカーが発動し、全分析を停止します。</div>
        </div>
      </div>
      <div class="flow-step">
        <span class="flow-num">STEP 2</span>
        <div class="flow-content">
          <div class="flow-title">全市場スクリーニング（183銘柄）</div>
          <div class="flow-desc">S&P500主要銘柄・Nasdaq100・日経225・ETFを対象に、RSI・出来高・モメンタムでフィルタリング。決算直前銘柄は自動除外されます。</div>
        </div>
      </div>
      <div class="flow-step">
        <span class="flow-num">STEP 3</span>
        <div class="flow-content">
          <div class="flow-title">マルチエージェント分析</div>
          <div class="flow-desc">上位5候補をSonnet 4.6（強気派・慎重派・リスク派）が議論し、Opus 4.6が最終判断を下します。スコア3.5以上かつ「買い」シグナルのみ通知されます。</div>
        </div>
      </div>
      <div class="flow-step">
        <span class="flow-num">STEP 4</span>
        <div class="flow-content">
          <div class="flow-title">Telegram通知 → 人間が判断</div>
          <div class="flow-desc">エントリー価格・目標株価・損切りライン・推奨株数（口座残高の10%基準）を通知。人間がマネックス証券アプリで発注します。</div>
        </div>
      </div>
      <div class="flow-step">
        <span class="flow-num">STEP 5</span>
        <div class="flow-content">
          <div class="flow-title">ポジション監視（5分ごと）</div>
          <div class="flow-desc">-5%で警告、-7%で損切りアラート、+10%で利確アラートを自動送信。損切りラインはAIが変更できない固定ルールです。</div>
        </div>
      </div>
    </div>
  </div>

  <div class="design-section">
    <h3>マルチエージェント設計</h3>
    <p>単一モデルの判断よりも、異なる視点を持つ複数のエージェントが議論することで、より堅牢な判断を実現します。</p>
    <div class="agent-grid">
      <div class="agent-card">
        <div class="agent-name" style="color:#00ff88">🟢 強気派 (Sonnet 4.6)</div>
        <div class="agent-desc">買うべき理由を3つ提示。テクニカルリバウンド・ファンダメンタルズ・セクタートレンドを根拠に楽観的ケースを構築。</div>
      </div>
      <div class="agent-card">
        <div class="agent-name" style="color:#ff4444">🔴 慎重派 (Sonnet 4.6)</div>
        <div class="agent-desc">買ってはいけない理由を3つ提示。下落トレンド継続・マクロリスク・業界逆風などの悲観的ケースを構築。</div>
      </div>
      <div class="agent-card">
        <div class="agent-name" style="color:#ffa500">⚠ リスク派 (Sonnet 4.6)</div>
        <div class="agent-desc">両派の議論の穴を指摘し最悪のシナリオを想定。ブラックスワンリスクや流動性リスクを評価。</div>
      </div>
      <div class="agent-card">
        <div class="agent-name" style="color:#0088ff">⚖ 審判 (Opus 4.6)</div>
        <div class="agent-desc">3つの意見を総合してスコア1-5と最終判断を出力。スコア3.5以上の「買い」のみシグナルとして送信。</div>
      </div>
    </div>
  </div>

  <div class="design-section">
    <h3>リスク管理原則</h3>
    <div class="risk-grid">
      <div class="risk-item">
        <div class="risk-label">損切りルール</div>
        <div class="risk-value">-7% 固定</div>
        <div style="font-size:11px;color:var(--dim);margin-top:4px">AIが変更不可。感情的な判断を排除するためハードコード。</div>
      </div>
      <div class="risk-item">
        <div class="risk-label">警告ライン</div>
        <div class="risk-value">-5%</div>
        <div style="font-size:11px;color:var(--dim);margin-top:4px">損切り前の早期警告。売り or 様子見を人間が判断。</div>
      </div>
      <div class="risk-item">
        <div class="risk-label">利確アラート</div>
        <div class="risk-value">+10%</div>
        <div style="font-size:11px;color:var(--dim);margin-top:4px">利確のタイミングを通知。AIが柔軟に上方修正も可能。</div>
      </div>
      <div class="risk-item green">
        <div class="risk-label">1トレード上限</div>
        <div class="risk-value">残高の10%</div>
        <div style="font-size:11px;color:var(--dim);margin-top:4px">口座残高から自動計算。過大なポジションを防止。</div>
      </div>
      <div class="risk-item green">
        <div class="risk-label">最大シグナル数</div>
        <div class="risk-value">1日5件</div>
        <div style="font-size:11px;color:var(--dim);margin-top:4px">過剰売買を防止。厳選されたシグナルのみ通知。</div>
      </div>
      <div class="risk-item blue">
        <div class="risk-label">サーキットブレーカー</div>
        <div class="risk-value">VIX &gt; 30</div>
        <div style="font-size:11px;color:var(--dim);margin-top:4px">市場パニック時は全分析停止。キャッシュポジションを維持。</div>
      </div>
    </div>
  </div>

  <div class="design-section">
    <h3>プロフェッショナルレビュー</h3>
    <p>このシステムの設計について、クオンツ・ファンドマネージャー視点でのレビューです。</p>
    <div class="risk-grid" style="grid-template-columns:repeat(2,1fr)">
      <div class="risk-item green">
        <div class="risk-label">✅ 正しい設計</div>
        <div class="risk-value" style="font-size:13px;margin-bottom:8px">Human-in-the-loop</div>
        <div style="font-size:11px;color:var(--dim)">完全自動売買を避け人間が最終判断する設計は正しい。AIの誤判断が即座に実損につながるリスクを排除している。</div>
      </div>
      <div class="risk-item green">
        <div class="risk-label">✅ 正しい設計</div>
        <div class="risk-value" style="font-size:13px;margin-bottom:8px">固定損切りルール</div>
        <div style="font-size:11px;color:var(--dim)">-7%損切りをハードコードしたことは最重要の正解。AIは常に「もう少し待てば回復する」理由を見つける。損切りをAIに委ねると破産する。</div>
      </div>
      <div class="risk-item green">
        <div class="risk-label">✅ 正しい設計</div>
        <div class="risk-value" style="font-size:13px;margin-bottom:8px">マルチエージェント議論</div>
        <div style="font-size:11px;color:var(--dim)">単一モデルより強気・慎重・リスクの3視点を持たせることで確証バイアスを軽減。Opusを審判に使う構成も適切。</div>
      </div>
      <div class="risk-item green">
        <div class="risk-label">✅ 正しい設計</div>
        <div class="risk-value" style="font-size:13px;margin-bottom:8px">サーキットブレーカー</div>
        <div style="font-size:11px;color:var(--dim)">VIX30超で全停止する設計はプロのリスク管理と一致。パニック相場での「バーゲンハンティング」衝動を封じる。</div>
      </div>
    </div>

    <div style="margin-top:20px">
    <div class="section-title" style="margin-bottom:12px">改善が必要な点</div>
    <div class="risk-grid" style="grid-template-columns:repeat(2,1fr)">
      <div class="risk-item">
        <div class="risk-label">⚠ 最重要課題</div>
        <div class="risk-value" style="font-size:13px;margin-bottom:8px">バックテストが未実施</div>
        <div style="font-size:11px;color:var(--dim)">「RSI &lt; 25で買い」が実際に有効かどうか、過去データで検証されていない。プロのシステムは必ず数年分のバックテストを行ってからライブ運用する。現在は「理論上は正しそう」という仮説で動かしている状態。</div>
      </div>
      <div class="risk-item">
        <div class="risk-label">⚠ 重要課題</div>
        <div class="risk-value" style="font-size:13px;margin-bottom:8px">セクター集中リスク</div>
        <div style="font-size:11px;color:var(--dim)">現在は上位5候補を機械的に選ぶため、テック株が全面安の日は5銘柄全てがテック株になる可能性がある。「同一セクター2銘柄まで」という制約が必要。</div>
      </div>
      <div class="risk-item">
        <div class="risk-label">⚠ 中程度課題</div>
        <div class="risk-value" style="font-size:13px;margin-bottom:8px">サバイバーシップバイアス</div>
        <div style="font-size:11px;color:var(--dim)">ティッカーリストは現在上場している銘柄のみ。過去に上場廃止・合併した銘柄は含まれないため、「RSI低下→回復」のパターンが過大評価されている可能性がある。</div>
      </div>
      <div class="risk-item">
        <div class="risk-label">⚠ 中程度課題</div>
        <div class="risk-value" style="font-size:13px;margin-bottom:8px">最大ドローダウン未管理</div>
        <div style="font-size:11px;color:var(--dim)">個別銘柄の損切りはあるが、口座全体の最大ドローダウンを管理していない。口座全体が-20%になっても売買を続けるリスクがある。「口座全体-15%で全ポジション清算」などのルールが必要。</div>
      </div>
    </div>
    </div>

    <div style="margin-top:20px">
    <div class="section-title" style="margin-bottom:12px">次のステップ（優先度順）</div>
    <div class="design-flow">
      <div class="flow-step">
        <span class="flow-num">P1</span>
        <div class="flow-content">
          <div class="flow-title">バックテスト実装</div>
          <div class="flow-desc">過去2-3年のデータでRSI戦略を検証する。勝率・平均損益・最大ドローダウンを計測し、現在の戦略が統計的に有意かどうかを確認する。</div>
        </div>
      </div>
      <div class="flow-step">
        <span class="flow-num">P2</span>
        <div class="flow-content">
          <div class="flow-title">セクター分散チェック</div>
          <div class="flow-desc">同一セクター2銘柄上限ルールをスクリーニングに追加。テック集中リスクを排除し、より安定したポートフォリオを実現する。</div>
        </div>
      </div>
      <div class="flow-step">
        <span class="flow-num">P3</span>
        <div class="flow-content">
          <div class="flow-title">口座全体のドローダウン管理</div>
          <div class="flow-desc">口座全体の損失が-15%を超えたら全ポジション清算・売買停止するルールを実装する。破産リスクの最終防衛ライン。</div>
        </div>
      </div>
      <div class="flow-step">
        <span class="flow-num">P4</span>
        <div class="flow-content">
          <div class="flow-title">少額で実運用開始</div>
          <div class="flow-desc">まず1-2ヶ月は1銘柄あたり10-20万円の少額で実運用。理論と現実のギャップを確認してから徐々に規模を拡大する。</div>
        </div>
      </div>
    </div>
    </div>
  </div>

  <div class="design-section">
    <h3>運用コスト</h3>
    <p>月間コストはClaude API（¥500-800）、Telegram（無料）、yfinance（無料）、MacBook M1電力（¥300-500）の合計で<strong style="color:var(--accent)">約¥800-1,300/月</strong>です。</p>
    <p>使用モデル: Claude Sonnet 4.6（強気派・慎重派・リスク派）× 3 + Claude Opus 4.6（審判）× 1 / シグナル</p>
  </div>

</div>

<div class="footer">ALMANAC v3.0 — 5戦略 × WFO最適化 × セクターローテーション — 5分ごと自動更新</div>

<script>
function showPage(name, el) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav a').forEach(a => a.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  el.classList.add('active');
}}

{charts_js}
</script>
</body>
</html>"""

    output = os.path.expanduser('~/portfolio-bot/dashboard.html')
    with open(output, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'ダッシュボード生成完了: {output}')

if __name__ == "__main__":
    generate()
