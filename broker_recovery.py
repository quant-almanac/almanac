"""Strict, read-only view of the legacy import journal. No recovery authority."""
import json
from pathlib import Path


class ImportRecoveryRequired(RuntimeError):
    pass


def read_import_operations(path: Path) -> dict[str, dict]:
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise ImportRecoveryRequired('broker_import_journal_unavailable') from exc
    operations = {}
    try:
        for line in raw.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError('row')
            op, status = row.get('operation_id'), row.get('status')
            if not isinstance(op, str) or not op.strip() or status not in {'prepared', 'committed'}:
                raise ValueError('identity/status')
            previous = operations.get(op)
            if status == 'committed':
                if previous is None:
                    raise ValueError('commit without prepare')
                row = {**previous, **row}
            elif previous is not None and previous['status'] == 'prepared':
                if previous.get('plan') != row.get('plan'):
                    raise ValueError('conflicting prepare')
            operations[op] = row
    except (ValueError, TypeError) as exc:
        raise ImportRecoveryRequired('broker_import_journal_invalid') from exc
    return operations


def require_import_recovery_clear(path: Path) -> None:
    if any(row['status'] != 'committed' for row in read_import_operations(path).values()):
        raise ImportRecoveryRequired('broker_import_recovery_required')


def read_import_recovery_status(path: Path) -> dict:
    try:
        rows = read_import_operations(path)
        count = sum(row['status'] != 'committed' for row in rows.values())
        return {'status': 'needs_reconciliation' if count else 'clear',
                'pending_count': count, 'execution_authorized': False,
                'reason': 'broker_import_recovery_required' if count else 'no_unresolved_broker_imports'}
    except ImportRecoveryRequired as exc:
        return {'status': 'unknown', 'pending_count': None,
                'execution_authorized': False, 'reason': str(exc)}
