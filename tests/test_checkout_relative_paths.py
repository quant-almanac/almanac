"""開発環境のパス直書きが再混入しないことの回帰テスト。

かつて 14 のモジュールが状態ファイルを固定パスで解決しており、別の場所へ
clone すると元の checkout を読み書きしていた。さらに download_tickers は
import しただけで tickers.json を書き換えるため、公開リポジトリのテストを
走らせるだけで別 checkout の状態ファイルが書き換わっていた。

文書で注意を促すのではなく、混入したら落ちるようにしてある。
"""
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# このファイル自身がヒットしないよう分割して組み立てる
LEGACY_PATH = "~/portfolio-" + "bot"


def _source_files():
    seen = set()
    for pattern in ("*.py", "*.sh"):
        for p in sorted(ROOT.glob(pattern)):
            seen.add(p)
    for pkg in ("almanac", "analyst", "api"):
        d = ROOT / pkg
        if d.is_dir():
            seen.update(d.rglob("*.py"))
    return sorted(seen)


def test_no_hardcoded_reference_deployment_path():
    offenders = []
    for p in _source_files():
        try:
            if LEGACY_PATH in p.read_text(encoding="utf-8"):
                offenders.append(str(p.relative_to(ROOT)))
        except (UnicodeDecodeError, OSError):
            continue
    assert offenders == [], (
        "開発環境のパスが直書きされています。状態ファイルは __file__ 起点で "
        f"解決してください: {offenders}"
    )


def test_download_tickers_import_does_not_write():
    """import しただけで tickers.json が書き換わらないこと。"""
    import download_tickers

    target = ROOT / "tickers.json"
    before = target.read_bytes() if target.exists() else None
    importlib.reload(download_tickers)
    after = target.read_bytes() if target.exists() else None
    assert after == before, "download_tickers の import が tickers.json を書き換えた"
