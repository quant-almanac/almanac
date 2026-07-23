"""
holdings.json の AVGO キーを日本語サフィックスから ASCII に移行するスクリプト。
  AVGO_特定  →  AVGO_toku
  AVGO_一般  →  AVGO_ippan

実行: python migrate_avgo_keys.py
"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
HOLDINGS_PATH = BASE_DIR / "holdings.json"

RENAME_MAP = {
    "AVGO_特定": "AVGO_toku",
    "AVGO_一般": "AVGO_ippan",
}


def migrate():
    with open(HOLDINGS_PATH, encoding="utf-8") as f:
        holdings = json.load(f)

    changed = False
    new_holdings = {}
    for key, value in holdings.items():
        new_key = RENAME_MAP.get(key, key)
        if new_key != key:
            print(f"  {key}  →  {new_key}")
            changed = True
        new_holdings[new_key] = value

    if not changed:
        print("変更なし（すでにマイグレーション済み、またはキーが存在しません）")
        return

    # アトミック書き込み
    import os, tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=HOLDINGS_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(new_holdings, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, HOLDINGS_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    print(f"✅ holdings.json を更新しました（{len(new_holdings)}件）")


if __name__ == "__main__":
    migrate()
