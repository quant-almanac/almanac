"""T1: FX TTL cache / stale fallback / source tag"""
import json
import time
from pathlib import Path

import utils


def test_fx_cache_hit(monkeypatch, tmp_path):
    utils._fx_cache_clear()
    calls = {'n': 0}
    acc = tmp_path / 'account.json'
    acc.write_text('{"fx_rate_usdjpy": 149.0}', encoding='utf-8')

    class _Fake:
        @property
        def fast_info(self):
            calls['n'] += 1
            return {'lastPrice': 150.5}

    class _FakeMod:
        def Ticker(self, _pair):
            return _Fake()

    monkeypatch.setitem(__import__('sys').modules, 'yfinance', _FakeMod())

    r1, s1 = utils.get_fx_rate_cached('USDJPY=X', account_json_path=acc)
    r2, s2 = utils.get_fx_rate_cached('USDJPY=X', account_json_path=acc)
    assert s1 == 'live'
    assert s2 == 'cache'
    assert calls['n'] == 1
    assert r1 == r2 == 150.5


def test_live_fx_refresh_recomputes_account_cash_totals(monkeypatch, tmp_path):
    utils._fx_cache_clear()
    calls = {'n': 0}
    acc = tmp_path / 'account.json'
    acc.write_text(json.dumps({
        "balance": 100_000,
        "usd_balance": 1_000,
        "fx_rate_usdjpy": 149.0,
        "jpy_equivalent_usd": 149_000,
        "total_cash": 249_000,
    }), encoding='utf-8')

    class _Fake:
        @property
        def fast_info(self):
            calls['n'] += 1
            return {'lastPrice': 151.25}

    class _FakeMod:
        def Ticker(self, _pair):
            return _Fake()

    monkeypatch.setitem(__import__('sys').modules, 'yfinance', _FakeMod())

    # persist_live_rate=True を明示した書き込み系だけが account.json を
    # 更新する。既定は False で、この関数の呼び出し元が dashboard/Today
    # のような読み取り専用リクエストの場合は財務状態を変更しない。
    r, src = utils.get_fx_rate_cached(
        'USDJPY=X', account_json_path=acc, persist_live_rate=True,
    )

    saved = json.loads(acc.read_text(encoding='utf-8'))
    assert (r, src) == (151.25, 'live')
    assert calls['n'] == 1
    assert saved["fx_rate_usdjpy"] == 151.25
    assert saved["jpy_equivalent_usd"] == 151_250
    assert saved["total_cash"] == 251_250


def test_live_fx_read_only_fetch_does_not_rewrite_account(monkeypatch, tmp_path):
    """persist_live_rate の既定 (False) では account.json を書き換えない。

    dashboard/portfolio/Today のような読み取り専用リクエストの経路
    (portfolio_manager.get_fx_rate() 経由) が、ライブ FX 取得という
    副作用だけで財務状態を変更してしまわないことを固定する。
    """
    utils._fx_cache_clear()
    acc = tmp_path / 'account.json'
    original = '{"balance": 1000, "usd_balance": 10, "fx_rate_usdjpy": 150}\n'
    acc.write_text(original, encoding='utf-8')

    class _Fake:
        @property
        def fast_info(self):
            return {'lastPrice': 151.25}

    class _FakeMod:
        def Ticker(self, _pair):
            return _Fake()

    monkeypatch.setitem(__import__('sys').modules, 'yfinance', _FakeMod())
    rate, source = utils.get_fx_rate_cached('USDJPY=X', account_json_path=acc)

    assert (rate, source) == (151.25, 'live')
    assert acc.read_text(encoding='utf-8') == original


def test_fx_stale_fallback(monkeypatch, tmp_path):
    utils._fx_cache_clear()
    acc = tmp_path / 'account.json'
    acc.write_text('{"fx_rate_usdjpy": 148.0}', encoding='utf-8')

    class _BadMod:
        def Ticker(self, *a, **k):
            raise RuntimeError('network down')

    monkeypatch.setitem(__import__('sys').modules, 'yfinance', _BadMod())
    r, src = utils.get_fx_rate_cached('USDJPY=X', account_json_path=acc)
    assert src == 'account_stale'
    assert r == 148.0


def test_fx_hardcoded(monkeypatch, tmp_path):
    utils._fx_cache_clear()
    acc = tmp_path / 'empty.json'  # missing

    class _BadMod:
        def Ticker(self, *a, **k):
            raise RuntimeError('x')

    monkeypatch.setitem(__import__('sys').modules, 'yfinance', _BadMod())
    r, src = utils.get_fx_rate_cached('USDJPY=X', account_json_path=acc)
    assert src == 'hardcoded'
    assert r == 150.0
