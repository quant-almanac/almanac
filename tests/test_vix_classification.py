from vix_classification import classify_vix, format_vix_level_ja, vix_macro_status


def test_classify_vix_uses_shared_thresholds():
    assert classify_vix(None) == "UNKNOWN"
    assert classify_vix(16.59) == "CALM"
    assert classify_vix(22.0) == "ELEVATED"
    assert classify_vix(31.0) == "HIGH_FEAR"
    assert classify_vix(45.0) == "EXTREME"


def test_vix_japanese_and_macro_labels_are_derived_from_same_classification():
    assert format_vix_level_ja(16.59) == "CALM（落ち着き）"
    assert vix_macro_status(16.59) == "normal"
    assert vix_macro_status(22.0) == "elevated"
    assert vix_macro_status(31.0) == "fear"
    assert vix_macro_status(45.0) == "capitulation"
