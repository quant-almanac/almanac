"""日証金 taisyaku.jp アダプタの受入 E2E(オフライン fixture)。

実データ(Shift_JIS)のヘッダ構造を模した fixture でパーサと sync を検証し、
sync 後に builder が「貸借銘柄かつ逆日歩なし」を shortable=true、逆日歩/申込停止/
非貸借を shortable=false にすることを端から端で確認する。外部 API には触れない。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sync_jp_short_taisyaku as tk

NOW = datetime(2026, 6, 26, 9, 0, 0)

_MEIGARA = (
    "貸借取引対象銘柄一覧,20260626,,,,,,,,,\n"
    "貸借申込日,コード,銘柄名,貸借銘柄区分（東証）,−,貸借銘柄区分（ＪＮＸ）,貸借銘柄区分（ＯＤＸ）,貸借銘柄区分（ＪＡＸ）,貸借銘柄区分（名証）,貸借銘柄区分（福証）,貸借銘柄区分（札証）\n"
    "20260629,7203,トヨタ,1,0,1,1,1,0,0,0\n"
    "20260629,9999,サンプル企業,2,0,1,1,1,0,0,0\n"
    "20260629,9997,非貸借,0,0,0,0,0,0,0,0\n"
)

_SHINA = (
    "品貸料率一覧,,,,,,,,,,,,,,,\n"
    "（注）取引所区分「東証」の…,,,,,,,,,,,,,,,\n"
    "抽出条件,全体,,,,,,,,,,,,,,\n"
    "貸借申込日,決済日,コード,銘柄名,取引所区分,決算事由,決算等,貸借値段（円）,貸株超過株数,最高料率（円）,当日品貸料率（円）,当日品貸日数,前日品貸料率（円）,備考,制限,応札倍率ランク\n"
    "20260625,20260629,8888,逆日歩銘柄,東証,決算,20260715,1000.00,50000,60.00,5.00,1,3.00,,,F\n"
    "20260625,20260629,7777,品貸ゼロ,東証,決算,20260715,1000.00,0,60.00,*****,*****,*****,,,\n"
)

_ZANDAKA = (
    '"申込日","決済日","銘柄コード","銘柄名","取引所区分名","上場区分","速報／確報","融資新規株数","融資返済株数","融資残高株数","貸株新規株数","貸株返済株数","貸株残高株数","差引残高株数"\n'
    '"2026/06/25","2026/06/29","7203","トヨタ","東証およびＰＴＳ",,"確報",0,0,100000,0,0,50000,50000\n'
    '"2026/06/25","2026/06/29","8888","逆日歩銘柄","東証およびＰＴＳ",,"確報",0,0,1000,0,0,9000,-8000\n'
)

_SEIGEN = (
    "貸借取引銘柄別制限措置等一覧\n"
    "本資料は情報提供を目的に…\n"
    "（注１）…\n"
    "（注２）…\n"
    "直近発表,銘柄コード,銘柄名,実施措置,実施内容,通知日・実施日,後場停止\n"
    ",5555,売禁銘柄,申込停止,新規売り,2025/01/01,\n"
    ",4444,制限銘柄,申込制限,,2025/01/01,\n"
    ",3333,注意銘柄,注意喚起,,2025/01/01,\n"
)

_TEXTS = {"meigara.csv": _MEIGARA, "shina.csv": _SHINA,
          "zandaka.csv": _ZANDAKA, "seigenichiran.csv": _SEIGEN}


# ── パーサ ────────────────────────────────────────────────

def test_parse_meigara_loanable_by_tse_kubun():
    m = tk.parse_meigara(_MEIGARA)
    assert m == {"7203.T": True, "9999.T": True, "9997.T": False}


def test_parse_shina_reverse_daily_fee():
    s = tk.parse_shina(_SHINA)
    assert s["8888.T"]["reverse_daily_fee"] is True
    assert s["8888.T"]["short_excess_shares"] == 50000
    assert s["7777.T"]["reverse_daily_fee"] is False


def test_parse_zandaka_loan_ratio():
    z = tk.parse_zandaka(_ZANDAKA)
    assert z["7203.T"]["loan_ratio"] == 2.0  # 100000/50000
    assert z["8888.T"]["loan_ratio"] < 1.0   # 1000/9000 踏み上げ警戒


def test_parse_seigen_regulation():
    g = tk.parse_seigen(_SEIGEN)
    assert g["5555.T"] == "short_ban"
    assert g["4444.T"] == "margin_up"
    assert g["3333.T"] == "caution"


# ── sync → ファイル出力 ───────────────────────────────────

def test_sync_writes_three_state_files(tmp_path):
    summary = tk.sync(base_dir=tmp_path, texts=_TEXTS, now=NOW)
    assert summary["loanable_count"] == 2  # 7203,9999
    assert summary["short_ban_count"] == 1
    for name in ("jp_loanable_state.json", "jsf_lending_state.json", "jp_regulation_state.json"):
        assert (tmp_path / "data" / name).exists()
    loan = json.loads((tmp_path / "data" / "jp_loanable_state.json").read_text(encoding="utf-8"))
    assert loan["loanable_by_ticker"]["7203.T"] is True


# ── 端から端: sync 後に builder が正しく判定 ──────────────

def test_builder_after_sync_classifies_jp_correctly(tmp_path):
    import short_universe as su
    tk.sync(base_dir=tmp_path, texts=_TEXTS, now=NOW)
    led = su.build_short_universe(
        ["7203.T", "8888.T", "5555.T", "9997.T", "9999.T"],
        now=NOW, base_dir=tmp_path,
    )
    t = led["tickers"]
    # 貸借銘柄・逆日歩なし・規制なし → shortable
    assert t["7203.T"]["shortable"] is True, t["7203.T"]["reasons"]
    # 逆日歩発生(踏み上げ済み) → 除外
    assert t["8888.T"]["shortable"] is False
    # 申込停止(short_ban) → 除外
    assert t["5555.T"]["shortable"] is False
    # 非貸借 → 除外
    assert t["9997.T"]["shortable"] is False
    # insider(勤務先株 9999.T)は貸借銘柄でも常に除外
    assert t["9999.T"]["shortable"] is False
    assert any("insider" in r.lower() for r in t["9999.T"]["reasons"])
    # 不変: 全件 observe_only / executable=False
    for v in t.values():
        assert v["human_execution_only"] is True
        assert v["executable"] is False
