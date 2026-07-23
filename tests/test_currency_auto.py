"""T12: actions.py 通貨自動判定 — .T/.JP は JPY、既存 holdings を優先"""
from api.routes.actions import _auto_detect_currency


def test_jp_suffix_auto():
    # holdings 空 → suffix で JPY 判定
    assert _auto_detect_currency('1489.T', {}) == 'JPY'
    assert _auto_detect_currency('7203.T', {}) == 'JPY'
    assert _auto_detect_currency('SOMETHING.JP', {}) == 'JPY'


def test_holdings_existing_preferred():
    holdings = {'IEV': {'ticker': 'IEV', 'currency': 'USD'}}
    # IEV は欧州 ETF だが USD 建て — 既存を優先
    assert _auto_detect_currency('IEV', holdings) == 'USD'


def test_unknown_ticker_returns_none():
    """新規 + .T/.JP 以外の suffix なし → None（API 側で 422 を返す）"""
    assert _auto_detect_currency('IEV', {}) is None
    assert _auto_detect_currency('NVDA', {}) is None
