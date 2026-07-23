import pytest

from instrument_metadata import (
    canonical_execution_ticker,
    canonical_ticker,
    quantity_label_for_ticker,
    trading_unit_for_ticker,
)


def test_jpx_aliases_are_canonicalized_without_touching_us_symbols() -> None:
    assert canonical_ticker("1489") == "1489.T"
    assert canonical_ticker("1306.JPX") == "1306.T"
    assert canonical_ticker("285A") == "285A.T"
    assert canonical_ticker("285A.JP") == "285A.T"
    assert canonical_ticker("285A.JPX") == "285A.T"
    assert canonical_ticker("xlf") == "XLF"


def test_held_jpx_etfs_use_official_trading_units() -> None:
    assert trading_unit_for_ticker("1489.T") == 1
    assert trading_unit_for_ticker("1306") == 10
    assert trading_unit_for_ticker("9999.T") == 100
    assert quantity_label_for_ticker("1489") == "口"
    assert quantity_label_for_ticker("9999.T") == "株"


def test_unknown_bare_alphanumeric_jpx_like_code_is_rejected_for_execution() -> None:
    with pytest.raises(ValueError):
        canonical_execution_ticker("286A")
