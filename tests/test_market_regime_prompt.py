import analyst


def _data():
    rate = {
        "status": "tightening_shock",
        "scope": "US_rates_as_global_equity_discount_modifier",
    }
    return {
        "market_regime_v2": {
            "portfolio": {
                "eligible": True,
                "committed_label": "mild_bear",
                "phase": "deteriorating",
            },
            "markets": {
                "US": {
                    "committed_label": "mild_bear",
                    "score": -22.0,
                    "rate_regime": rate,
                },
                "JP": {
                    "committed_label": "neutral",
                    "score": 4.0,
                    "rate_regime": rate,
                },
            },
            "shock": {"active": False},
            "policy": {
                "cash_target_pct": 20.0,
                "cash_action": "target",
                "buy_size_multiplier": 0.25,
            },
        }
    }


def test_private_regime_context_includes_policy_and_long_rate_modifier():
    text = analyst._compute_regime_consensus(_data())

    assert "US=mild_bear" in text
    assert "JP=neutral" in text
    assert "cash_target=20.0%" in text
    assert "US長期金利" in text
    assert "tightening_shock" in text


def test_public_regime_context_excludes_portfolio_policy():
    text = analyst._compute_regime_consensus(_data(), public_only=True)

    assert "US=mild_bear" in text
    assert "JP=neutral" in text
    assert "cash_target" not in text
    assert "portfolio=" not in text
