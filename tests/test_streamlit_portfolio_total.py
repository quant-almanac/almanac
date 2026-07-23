import importlib
import sys
import types


def _load_streamlit_app(monkeypatch, *, streamlit_module=None, anthropic_module=None):
    class _FakeStreamlit(types.SimpleNamespace):
        def __init__(self):
            super().__init__(session_state={})

        def cache_data(self, *args, **kwargs):
            def _decorator(fn):
                return fn
            return _decorator

        def set_page_config(self, *args, **kwargs):
            return None

        def __getattr__(self, _name):
            def _noop(*args, **kwargs):
                return None
            return _noop

    monkeypatch.setitem(sys.modules, "streamlit", streamlit_module or _FakeStreamlit())
    monkeypatch.setitem(sys.modules, "anthropic", anthropic_module or types.SimpleNamespace())
    sys.modules.pop("streamlit_app", None)
    return importlib.import_module("streamlit_app")


def test_get_portfolio_total_prefers_portfolio_manager_snapshot(monkeypatch):
    app = _load_streamlit_app(monkeypatch)
    calls = []

    class _PortfolioManager:
        def build_portfolio_snapshot(self, **kwargs):
            calls.append(kwargs)
            return {"total_jpy": 12_345_678}

    monkeypatch.setattr(app, "portfolio_mgr", _PortfolioManager())
    monkeypatch.setattr(app, "load_account", lambda: {
        "balance": 100_000,
        "usd_balance": 1_000,
        "fx_rate_usdjpy": 151.25,
        "total_cash": 999_999_999,
    })
    monkeypatch.setattr(app, "load_holdings", lambda: {
        "AAPL": {"shares": 100, "entry_price": 100, "currency": "USD"},
    })

    assert app.get_portfolio_total() == 12_345_678
    assert calls == [{"fetch_missing_sectors": False}]


def test_get_portfolio_total_fallback_avoids_cash_mirror_double_count(monkeypatch):
    app = _load_streamlit_app(monkeypatch)
    monkeypatch.setattr(app, "portfolio_mgr", None)
    monkeypatch.setattr(app, "load_account", lambda: {
        "balance": 100.0,
        "usd_balance": 10.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash": 999_999.0,
    })
    monkeypatch.setattr(app, "load_holdings", lambda: {
        "CASH_JPY": {"shares": 100.0, "entry_price": 1.0, "currency": "JPY"},
        "CASH_USD": {"shares": 10.0, "entry_price": 1.0, "currency": "USD"},
        "CASH_JPY_SBI": {"shares": 50.0, "entry_price": 1.0, "currency": "JPY"},
        "AAPL": {"shares": 1.0, "entry_price": 2.0, "currency": "USD"},
        "7203.T": {"shares": 10.0, "entry_price": 20.0, "currency": "JPY"},
    })

    # account cash: 100 + 10 * 150 = 1600
    # non-account cash mirror: CASH_JPY_SBI = 50
    # positions: AAPL = 1 * 2 * 150 = 300, 7203.T = 10 * 20 = 200
    assert app.get_portfolio_total() == 2_150


def test_holding_value_jpy_handles_cash_usd_and_domestic_funds(monkeypatch):
    app = _load_streamlit_app(monkeypatch)

    assert app._holding_value_jpy(
        "CASH_JPY",
        {"shares": 100_000, "entry_price": 1, "currency": "JPY"},
        150.0,
    ) == 0
    assert app._holding_value_jpy(
        "AAPL",
        {"shares": 2, "entry_price": 100, "currency": "USD"},
        150.0,
    ) == 30_000
    assert app._holding_value_jpy(
        "SLIM_SP500",
        {"shares": 10_000, "current_nav": 25_000, "entry_price": 20_000, "currency": "JPY", "unit": "口"},
        150.0,
    ) == 25_000


def test_ai_explain_logs_stream_usage(monkeypatch):
    rows: list[dict] = []

    class _Placeholder:
        def markdown(self, *args, **kwargs):
            return None

    class _FakeColumn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, exc_tb):
            return None

    class _FakeStreamlit(types.SimpleNamespace):
        def __init__(self):
            super().__init__(session_state={"ai_explain_text_demo": "__loading__"})

        def cache_data(self, *args, **kwargs):
            def _decorator(fn):
                return fn
            return _decorator

        def set_page_config(self, *args, **kwargs):
            return None

        def columns(self, *args, **kwargs):
            return [_FakeColumn(), _FakeColumn()]

        def button(self, *args, **kwargs):
            return False

        def empty(self):
            return _Placeholder()

        def rerun(self):
            return None

        def __getattr__(self, _name):
            def _noop(*args, **kwargs):
                return None
            return _noop

    class FakeStream:
        text_stream = ["hello", " world"]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, exc_tb):
            return None

        def get_final_message(self):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text")],
                stop_reason="end_turn",
                usage=types.SimpleNamespace(input_tokens=77, output_tokens=14),
            )

    class FakeMessages:
        def stream(self, **kwargs):
            return FakeStream()

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    fake_st = _FakeStreamlit()
    app = _load_streamlit_app(
        monkeypatch,
        streamlit_module=fake_st,
        anthropic_module=types.SimpleNamespace(Anthropic=FakeAnthropicClient),
    )
    monkeypatch.setattr(app, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    app._render_ai_explain("損益", {"value": 123}, "demo")

    assert fake_st.session_state["ai_explain_text_demo"] == "hello world"
    assert rows, "Streamlit AI explain calls should log final Anthropic usage"
    row = rows[-1]
    assert row["role"] == "streamlit_ai_explain"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["section_label"] == "損益"
    assert row["status"] == "ok"
    assert row["input_tokens"] == 77
    assert row["output_tokens"] == 14
