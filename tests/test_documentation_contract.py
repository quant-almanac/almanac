from scripts.check_docs_consistency import check


def test_bilingual_specs_and_module_catalog_are_complete() -> None:
    assert check() == []
