from __future__ import annotations

import analyst


class _Output:
    n_hypotheses_total = 1


def test_catalyst_context_mode_off_keeps_log_run_but_skips_opus_injection(monkeypatch):
    calls = []
    monkeypatch.setenv("ALMANAC_CATALYST_CONTEXT_MODE", "off")
    monkeypatch.setattr(
        "almanac.observability.catalyst_layer.run",
        lambda **kwargs: calls.append(kwargs) or _Output(),
    )
    monkeypatch.setattr("almanac.observability.catalyst_layer.compact_for_opus", lambda *_args, **_kwargs: "context")
    assert analyst._load_catalyst_context_for_opus(analysis_id="test-analysis") == ""
    assert calls and calls[0]["write_log"] is True


def test_catalyst_context_defaults_to_on(monkeypatch):
    monkeypatch.delenv("ALMANAC_CATALYST_CONTEXT_MODE", raising=False)
    monkeypatch.delenv("ALMANAC_DISABLE_CATALYST_CONTEXT", raising=False)
    monkeypatch.setattr("almanac.observability.catalyst_layer.run", lambda **_kwargs: _Output())
    monkeypatch.setattr("almanac.observability.catalyst_layer.compact_for_opus", lambda *_args, **_kwargs: "context")
    assert analyst._load_catalyst_context_for_opus(analysis_id="test-analysis") == "context"
