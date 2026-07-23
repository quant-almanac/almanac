"""
共通ユーティリティ
"""
import contextlib
import errno
import fcntl
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Iterable, Iterator, Optional, Tuple

from almanac.runtime_config import default_secrets_paths, env_bool, get_env

# ── yfinance グローバル timeout ──────────────────────────
YF_TIMEOUT = 30  # 秒

# ── FX レートキャッシュ（P0-1） ──────────────────────────
# TTL 付き in-memory cache + account.json への stale fallback
FX_CACHE_TTL_SEC = 600            # 10 分
FX_HARDCODED_FALLBACK = 150.0     # 最終的な最悪フォールバック
_fx_cache: dict = {}              # {pair: (rate, fetched_at)}

_logger = logging.getLogger(__name__)


def init_yfinance_timeout(timeout: int = YF_TIMEOUT) -> None:
    """yfinance のグローバル curl_cffi セッションに timeout を設定する。
    各スクリプトの冒頭で1回呼ぶだけで全 yf.Ticker / yf.download に適用される。
    """
    try:
        from yfinance.data import YfData
        d = YfData()
        d._session.timeout = timeout
    except Exception:
        pass  # yfinance 未インストール環境でも安全


def reset_yfinance_session(timeout: int = YF_TIMEOUT) -> None:
    """yfinance の singleton session を閉じて作り直す。

    yfinance/curl_cffi は長時間常駐プロセスで CLOSE_WAIT socket を保持することがある。
    alert.py のような daemon は定期的に session を作り直し、FD 枯渇を避ける。
    """
    try:
        from curl_cffi import requests as curl_requests
        from yfinance.data import YfData

        data = YfData()
        old_session = getattr(data, "_session", None)
        if old_session is not None:
            try:
                old_session.close()
            except Exception:
                pass
        data._set_session(curl_requests.Session(impersonate="chrome"))
        init_yfinance_timeout(timeout)
    except Exception:
        pass  # yfinance 未インストール/内部 API 変更時も常駐処理を止めない


def load_json(path, default=None):
    """JSONファイルを安全に読み込む。ファイル不在やパースエラー時は default を返す。

    fail-silent な挙動。screener や news のような「壊れても致命的でない」用途向け。
    holdings.json / account.json など台帳系は load_json_strict を使うこと。
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def load_json_strict(path):
    """
    P1-14: fail-closed 版の JSON 読み込み。

    holdings.json / account.json / cash_transactions.json / nisa_portfolio.json など、
    破損したまま空 dict で続行すると静かに資産状態が「ポジションなし」「設定なし」として
    進む危険ファイル向け。

    ファイル不在は FileNotFoundError、JSON パースエラーは ValueError として明示的に raise する。
    呼び出し側で「初回起動の不在は許容、破損は止める」のように分離できる。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"必須ファイルが存在しません: {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"{p} の読み込みに失敗: {e}") from e
    if not text.strip():
        raise ValueError(f"{p} が空です（破損の可能性）")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{p} の JSON パース失敗: {e}。"
            " backups/ から最新版を確認し restore コマンドで復元してください。"
        ) from e


def _parse_secret_assignment(line: str) -> tuple[str, str] | None:
    import re

    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].strip()
    if "=" not in stripped:
        return None
    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
        return None

    value = raw_value.strip()
    if value.startswith(("'", '"')):
        quote = value[0]
        end = value.find(quote, 1)
        value = value[1:end] if end >= 1 else value[1:]
    else:
        value = value.split("#", 1)[0].strip()
    return key, value


def load_environment_secrets(
    *,
    paths: Iterable[Path | str] | None = None,
    override: bool = False,
) -> set[str]:
    """Load local shell-style secret files into os.environ without printing values.

    The default path is ``~/.almanac_secrets`` with ``~/.nexustrader_secrets``
    as a legacy fallback. Tests or local tooling can override this with
    ``ALMANAC_SECRETS_FILE`` or legacy ``NEXUSTRADER_SECRETS_FILE``.
    """
    if paths is None:
        paths = default_secrets_paths()

    loaded: set[str] = set()
    for raw_path in paths:
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            _logger.warning("[secrets] 読み込み失敗: %s", exc)
            continue
        for line in lines:
            parsed = _parse_secret_assignment(line)
            if parsed is None:
                continue
            key, value = parsed
            if not override and os.environ.get(key):
                continue
            os.environ[key] = value
            loaded.add(key)
    return loaded


def normalize_account_cash_derived_totals(account: dict) -> tuple[dict, bool]:
    """Return account with cached JPY cash totals recomputed from balances and FX."""
    if not isinstance(account, dict):
        return account, False
    try:
        jpy = float(account.get("balance", 0) or 0)
        usd = float(account.get("usd_balance", 0) or 0)
        fx = float(account.get("fx_rate_usdjpy", 0) or 0)
    except (TypeError, ValueError):
        return account, False
    if fx <= 0:
        return account, False

    normalized = dict(account)
    expected_usd_jpy = int(round(usd * fx))
    expected_total = int(round(jpy + usd * fx))
    changed = (
        normalized.get("jpy_equivalent_usd") != expected_usd_jpy
        or normalized.get("total_cash") != expected_total
    )
    normalized["jpy_equivalent_usd"] = expected_usd_jpy
    normalized["total_cash"] = expected_total
    return normalized, changed


def sync_account_cash_derived_totals(account_json_path: Optional[Path] = None) -> bool:
    """Persist normalized derived cash totals in account.json when they drift."""
    acc_path = account_json_path or (Path(__file__).parent / "account.json")
    if not acc_path.exists():
        return False
    account = load_json(acc_path, default={})
    normalized, changed = normalize_account_cash_derived_totals(account)
    if changed:
        atomic_write_json(acc_path, normalized)
    return bool(changed)


def get_fx_rate_cached(pair: str = 'USDJPY=X',
                       ttl_sec: int = FX_CACHE_TTL_SEC,
                       account_json_path: Optional[Path] = None,
                       ) -> Tuple[float, str]:
    """
    USD/JPY（またはその他通貨ペア）の FX レートを TTL キャッシュ付きで取得する。

    優先順位:
      1. TTL 内のメモリキャッシュ（source='cache'）
      2. yfinance ライブ値（source='live'）→ キャッシュに格納、account.json にも保存
      3. account.json の fx_rate_usdjpy（source='account_stale'）→ warning ログ
      4. FX_HARDCODED_FALLBACK=150.0（source='hardcoded'）→ error ログ

    Args:
        pair: 通貨ペア（デフォルト USDJPY=X）
        ttl_sec: キャッシュ TTL 秒数
        account_json_path: account.json のパス（テスト時に上書き可）

    Returns:
        (rate: float, source: str)
        source ∈ {'cache','live','account_stale','hardcoded'}
    """
    now = time.time()
    # 1. メモリキャッシュ
    cached = _fx_cache.get(pair)
    if cached is not None:
        rate, fetched_at = cached
        if now - fetched_at < ttl_sec:
            return float(rate), 'cache'

    # 2. yfinance ライブ値
    try:
        import yfinance as yf
        rate = float(yf.Ticker(pair).fast_info['lastPrice'])
        if rate > 0 and rate < 1000:  # sanity: USDJPY は 50-500 の範囲を外れたら異常
            _fx_cache[pair] = (rate, now)
            # account.json にも保存して stale fallback を鮮度高く保つ
            try:
                acc_path = account_json_path or (Path(__file__).parent / 'account.json')
                if acc_path.exists():
                    acc = load_json(acc_path, default={})
                    if pair == 'USDJPY=X':
                        acc['fx_rate_usdjpy'] = rate
                        acc['fx_rate_usdjpy_as_of'] = now
                        try:
                            jpy = float(acc.get('balance', 0) or 0)
                            usd = float(acc.get('usd_balance', 0) or 0)
                        except (TypeError, ValueError):
                            pass
                        else:
                            acc, _changed = normalize_account_cash_derived_totals(acc)
                        atomic_write_json(acc_path, acc)
            except Exception as e:
                _logger.debug(f"[fx] account.json 更新失敗（無害）: {e}")
            return rate, 'live'
    except Exception as e:
        _logger.warning(f"[fx] yfinance 取得失敗（stale fallback に切替）: {e}")

    # 3. account.json の stale 値
    try:
        acc_path = account_json_path or (Path(__file__).parent / 'account.json')
        acc = load_json(acc_path, default={})
        stale_rate = acc.get('fx_rate_usdjpy')
        if stale_rate and 50 < float(stale_rate) < 500:
            _logger.warning(f"[fx] stale fallback 使用: {stale_rate} (from account.json)")
            return float(stale_rate), 'account_stale'
    except Exception as e:
        _logger.warning(f"[fx] account.json 読込失敗: {e}")

    # 4. 最終フォールバック
    _logger.error(f"[fx] ハードコードフォールバック使用: {FX_HARDCODED_FALLBACK}")
    return FX_HARDCODED_FALLBACK, 'hardcoded'


def _fx_cache_clear() -> None:
    """テスト用: FX キャッシュをクリア"""
    _fx_cache.clear()


# ── 再現性（P3-16） ──────────────────────────
# ALMANAC_DETERMINISTIC=1 で numpy/torch/sklearn の乱数 seed を固定。
# LLM 呼出は analyst/llm_client.py の call_claude が deterministic 時に temperature=0。

DEFAULT_SEED = 42


def is_deterministic_mode() -> bool:
    """ALMANAC_DETERMINISTIC=1 or ALMANAC_SEED があれば deterministic モード。"""
    return env_bool("ALMANAC_DETERMINISTIC") or (
        get_env("ALMANAC_SEED") is not None
    )


def set_global_seeds(seed: int = DEFAULT_SEED) -> None:
    """
    numpy / torch / sklearn / random / yfinance rng の乱数 seed を統一する。
    deterministic モードでない場合は何もしない。
    """
    if not is_deterministic_mode():
        return
    import random as _py_random
    _py_random.seed(seed)
    try:
        import numpy as _np
        _np.random.seed(seed)
    except ImportError:
        pass
    try:
        import torch as _torch
        _torch.manual_seed(seed)
        if _torch.cuda.is_available():
            _torch.cuda.manual_seed_all(seed)
        _torch.backends.cudnn.deterministic = True
        _torch.backends.cudnn.benchmark = False
    except Exception:
        pass
    os.environ.setdefault('PYTHONHASHSEED', str(seed))
    _logger.info(f'[determinism] seeds={seed}')


def get_llm_temperature(default: float = 0.0) -> float:
    """
    LLM 呼出の温度を返す。deterministic モード時は常に 0、
    そうでなければ呼出側が渡した default 値を返す。
    """
    return 0.0 if is_deterministic_mode() else default


# ── ハートビート（P2-9） ──────────────────────────
HEARTBEAT_PATH = Path(__file__).parent / 'heartbeats.json'


def heartbeat(script_name: str,
              status: str = 'ok',
              error: Optional[str] = None,
              extra: Optional[dict] = None) -> None:
    """
    スクリプトの生存シグナルを heartbeats.json に記録する。

    watchdog.py が定期的にこのファイルを読み、想定周期を超過したスクリプトや
    status='error' のスクリプトを Telegram で通知する。

    Args:
        script_name: スクリプト名（例: 'analyzer', 'data_fetcher'）
        status: 'ok' | 'error' | 'warn'
        error: エラー時のメッセージ
        extra: 任意の追加情報（dict）
    """
    try:
        data = load_json(HEARTBEAT_PATH, default={})
        data[script_name] = {
            'last_run_ts': time.time(),
            'last_run_iso': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'status': status,
            'error': error,
            'extra': extra or {},
        }
        atomic_write_json(HEARTBEAT_PATH, data)
    except Exception as e:
        _logger.warning(f"[heartbeat] 書込失敗 {script_name}: {e}")


# ── プロセス間排他 (P1-15) ──────────────────────────
# uvicorn --reload や launchd と API の同時実行で BG ジョブが二重起動するのを防ぐ。
# 旧実装はモジュール内 bool (_refresh_running) で、reload や複数プロセスで破綻していた。
# fcntl.flock は POSIX で、macOS/Linux で動作。Windows は msvcrt が必要だが本プロジェクトは macOS only。

LOCKS_DIR = Path(__file__).parent / 'locks'


class LockBusy(RuntimeError):
    """別プロセスが既にロックを保持している。"""


@contextlib.contextmanager
def process_lock(name: str, *, timeout: float = 0.0) -> Iterator[Path]:
    """
    OS レベルの排他ロック (fcntl.flock LOCK_EX + LOCK_NB)。

    使い方:
        try:
            with process_lock('ai_analysis'):
                run_analysis(...)
        except LockBusy:
            # 別プロセスが分析中

    Args:
        name:    ロック名 (e.g. 'ai_analysis')。ファイル名に使われる
        timeout: 非ゼロなら timeout 秒だけ取得を再試行。デフォルト 0=即時 raise

    Raises:
        LockBusy: timeout 内に取得できなかった場合
    """
    LOCKS_DIR.mkdir(exist_ok=True)
    lock_path = LOCKS_DIR / f'{name}.lock'
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)

    acquired = False
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise
                if timeout <= 0 or time.time() >= deadline:
                    raise LockBusy(f"lock '{name}' busy") from e
                time.sleep(0.1)

        # PID とタイムスタンプを書き込む (デバッグ用)
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()} {time.strftime('%Y-%m-%dT%H:%M:%S')}\n".encode())
        except OSError:
            pass

        yield lock_path
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


def is_locked(name: str) -> bool:
    """
    ロック状態の非破壊チェック。LOCK_NB を試して失敗したら locked。
    """
    lock_path = LOCKS_DIR / f'{name}.lock'
    if not lock_path.exists():
        return False
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True
            raise
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def atomic_write_json(path, data: dict, **kwargs) -> None:
    """
    JSONファイルをアトミックに書き込む。

    tempファイルに書き込んでから os.replace() でアトミックに置き換えることで、
    書き込み途中のクラッシュやプロセス間レースコンディションを防ぐ。

    Args:
        path:   書き込み先パス (str or Path)
        data:   シリアライズ対象の dict
        **kwargs: json.dump に渡す追加引数
    """
    path = Path(path)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, **kwargs)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
