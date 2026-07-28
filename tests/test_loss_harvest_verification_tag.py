"""Stage 5C: public prompt exposes cost-basis verification status."""


def test_missing_crosscheck_is_unverified():
    from analyst import _loss_harvest_verification_tag

    assert _loss_harvest_verification_tag({}) == "台帳未確認"


def test_available_crosscheck_is_verified():
    from analyst import _loss_harvest_verification_tag

    candidate = {"cost_basis_crosscheck": {"available": True, "data_quality_issues": []}}
    assert _loss_harvest_verification_tag(candidate) == "台帳確認済"


def test_quality_issues_are_visible():
    from analyst import _loss_harvest_verification_tag

    candidate = {
        "cost_basis_crosscheck": {
            "available": True,
            "data_quality_issues": ["missing_opening_balance"],
        }
    }
    assert _loss_harvest_verification_tag(candidate) == "台帳確認済・品質懸念あり"
