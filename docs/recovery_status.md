# Recovery diagnostics

The System page reports execution-application and balance-import recovery
separately. Missing/unreadable evidence is unknown, not zero. No override control
or execution permission is provided.

New import intents retain both before and after images. Recovery verifies both
files before changing either. Divergence, multiple pending imports, malformed
journal records and ambiguous legacy state require reconciliation. Historical
completed repeats remain readable; completed input is not reapplied.

Execution fill intake retains a pending fact when blocked. Cash changes rejected
with HTTP 409 are unapplied, not durably queued; retain the original source for
later reconciliation. The shared lock/barrier also covers position import,
holdings CRUD, alert flag writes, legacy commands and maintenance roll-forward.
This does not redesign every writer's own crash recovery or provide a global
filesystem transaction. One-off migrations and optional legacy FX persistence
must not run against live state during recovery.

Analysis observations are source-bound diagnostics only, saved after the formal
cache. Independent stage lists are not a conserved funnel. Observations cannot
alter selection, risk policy, history or execution authority. NAV publication
monitoring is likewise separate from effective-NAV/drawdown qualification.

Deploy the schema/backend before the UI. Preserve recovery journals, original
facts and consistent database backups. Do not erase a pending record or restore
individual financial files merely to clear a warning. Test and build results
are not proof of deployment or a successful scheduled analysis.
