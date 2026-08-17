from api.routes import today


def test_pulse_passes_through_vix_history_when_present():
    macro = {"vix": 14.9}
    vix_state = {
        "cached_at": "2026-08-10T06:00:00",
        "vix": {
            "change_1d": -1.65, "change_5d": -6.82, "decay_from_peak_10d_pct": -27.88,
            "history_1mo": [{"date": "2026-07-10", "close": 15.03}, {"date": "2026-08-10", "close": 14.9}],
        },
    }
    pulse = today._build_pulse(macro, vix_state)
    assert pulse["vix"] == 14.9
    assert pulse["vix_change_1d"] == -1.65
    assert pulse["vix_change_5d"] == -6.82
    assert pulse["vix_decay_from_peak_10d_pct"] == -27.88
    assert pulse["as_of"] == "2026-08-10T06:00:00"
    assert pulse["vix_history_1mo"] == [
        {"date": "2026-07-10", "close": 15.03}, {"date": "2026-08-10", "close": 14.9},
    ]


def test_pulse_passes_through_oil_10y_dxy_history_when_present():
    """VIXは「市場の鼓動」の一例 — 原油・米10年債・DXYも同じ1moバッチ由来の実系列を素通しする。"""
    macro = {"vix": 14.9}
    vix_state = {
        "cached_at": "2026-08-10T06:00:00",
        "vix": {"change_1d": -1.65, "change_5d": -6.82},
        "oil": {
            "price": 79.49, "change_1d_pct": 0.14, "change_5d_pct": -1.2,
            "history_1mo": [{"date": "2026-08-10", "close": 79.49}],
        },
        "yields": {
            "us_10y": 4.66, "us_10y_change_1d_pt": -0.01, "us_10y_change_5d_pt": 0.05,
            "us_10y_history_1mo": [{"date": "2026-08-10", "close": 4.66}],
        },
        "dxy": {
            "level": 99.66, "change_1d_pct": 0.06, "change_5d_pct": -0.3,
            "history_1mo": [{"date": "2026-08-10", "close": 99.66}],
        },
    }
    pulse = today._build_pulse(macro, vix_state)
    assert pulse["oil_price"] == 79.49
    assert pulse["oil_change_1d_pct"] == 0.14
    assert pulse["oil_history_1mo"] == [{"date": "2026-08-10", "close": 79.49}]
    assert pulse["us_10y"] == 4.66
    assert pulse["us_10y_change_1d_pt"] == -0.01
    assert pulse["us_10y_history_1mo"] == [{"date": "2026-08-10", "close": 4.66}]
    assert pulse["dxy_level"] == 99.66
    assert pulse["dxy_change_1d_pct"] == 0.06
    assert pulse["dxy_history_1mo"] == [{"date": "2026-08-10", "close": 99.66}]


def test_pulse_oil_10y_dxy_default_to_empty_when_keys_absent():
    """oil/yields/dxy サブ辞書そのものが無い(古い形の vix_state)でも例外にならない。"""
    pulse = today._build_pulse({"vix": 14.9}, {"vix": {}, "cached_at": None})
    assert pulse["oil_price"] is None
    assert pulse["oil_history_1mo"] == []
    assert pulse["us_10y"] is None
    assert pulse["us_10y_history_1mo"] == []
    assert pulse["dxy_level"] is None
    assert pulse["dxy_history_1mo"] == []


def test_pulse_defaults_history_to_empty_list_for_legacy_cache_without_it():
    """移行処理は追加しない — 旧キャッシュ(history_1moキー無し)でも壊れず、空配列を返す。"""
    macro = {"vix": 20.1}
    vix_state = {"cached_at": "2026-08-01T06:00:00", "vix": {"change_1d": 0.5, "change_5d": 1.2}}
    pulse = today._build_pulse(macro, vix_state)
    assert pulse["vix_history_1mo"] == []
    assert pulse["vix"] == 20.1
    assert pulse["vix_change_1d"] == 0.5


def test_pulse_survives_completely_empty_vix_state():
    """get_vix_context() の完全失敗フォールバック({} 相当)でも例外を出さない。"""
    pulse = today._build_pulse({}, {})
    assert pulse["vix"] is None
    assert pulse["vix_change_1d"] is None
    assert pulse["vix_change_5d"] is None
    assert pulse["as_of"] is None
    assert pulse["vix_history_1mo"] == []


def test_pulse_survives_vix_key_explicitly_none():
    """vix_state["vix"] が None (辞書ではなく) でも history_1mo アクセスで例外にならない。"""
    pulse = today._build_pulse({"vix": None}, {"vix": None, "cached_at": None})
    assert pulse["vix"] is None
    assert pulse["vix_history_1mo"] == []


def test_pulse_does_not_synthesize_history_from_change_fields():
    """change_1d/5d はあっても history_1mo が無いなら合成せず空配列のまま
    (旧「5日前・1日前・現在」の3点合成はフロント側で廃止済みだが、
    バックエンドがここで代わりに合成し始めることも禁止する)。"""
    macro = {"vix": 30.0}
    vix_state = {"vix": {"change_1d": 5.0, "change_5d": 12.0}, "cached_at": "2026-08-05T06:00:00"}
    pulse = today._build_pulse(macro, vix_state)
    assert pulse["vix_history_1mo"] == []
