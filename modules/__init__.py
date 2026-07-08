"""
RecruitOS modular extensions.

Each new platform module (CRM, Business Development, Billing, etc.) lives in its
own file inside this package as a Flask Blueprint. This keeps the original ATS
(server.py) untouched while letting the platform grow into many independent,
testable modules that all share ONE database, ONE login and ONE activity
timeline.

Design contract for every module in this package:
  - It exposes a Blueprint named `bp`.
  - It NEVER imports heavy state at module load time; it calls shared helpers
    (get_db, current_user, ...) lazily via `modules.shared` so there is no
    circular-import problem with server.py.
  - It registers its own tables through `register_migration(fn)` so the schema
    is created/updated on startup alongside the core tables.
  - Every create/update/delete writes to the universal activity + audit log.

`register_all(app)` is called once from server.py to mount every module.
"""

from importlib import import_module

# Modules to mount, in load order. Add new module filenames here (without .py).
_MODULE_NAMES = [
    'crm',
    'bd',
    'xp',
    'wa_agent',
    'freelancer',
]

_MIGRATIONS = []
_IMPORTED = False


def import_all_modules():
    """Import every module once so their @register_migration decorators fire.
    Separated from register_all so migrations can run before the Flask app is
    finalised. Safe to call multiple times."""
    global _IMPORTED
    if _IMPORTED:
        return
    for name in _MODULE_NAMES:
        try:
            import_module(f'modules.{name}')
        except Exception as e:
            print(f'[modules] failed to import {name}: {e}')
    _IMPORTED = True


def register_migration(fn):
    """Modules call this to have their schema built on startup."""
    _MIGRATIONS.append(fn)
    return fn


def run_migrations(conn):
    """Invoked from server.py init_db() after core tables are ready."""
    for fn in _MIGRATIONS:
        try:
            fn(conn)
        except Exception as e:  # never let one module's migration crash boot
            print(f'[modules] migration failed for {getattr(fn, "__name__", fn)}: {e}')


def register_all(app):
    """Import each module (if not already) and register its Blueprint."""
    import_all_modules()
    for name in _MODULE_NAMES:
        try:
            mod = import_module(f'modules.{name}')
            if hasattr(mod, 'bp'):
                app.register_blueprint(mod.bp)
                print(f'[modules] mounted: {name}')
        except Exception as e:
            print(f'[modules] failed to mount {name}: {e}')
