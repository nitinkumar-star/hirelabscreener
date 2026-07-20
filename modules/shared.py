"""
Shared access layer for all RecruitOS modules.

server.py imports `modules` (to register blueprints), so a module importing
server.py at load time would create a circular import. To avoid that, modules
import helpers from here, and this file resolves them from the already-loaded
server module at call time (lazily).

This is the single seam between the legacy ATS core and the new modular platform.
Every module goes through these functions — never touches server.py internals
directly — so the core stays swappable.
"""

import sys


def _core():
    """Return the loaded server.py module object (it's __main__ under gunicorn/py)."""
    # server.py runs as the entry module; it may be '__main__' or 'server'.
    for key in ('server', '__main__'):
        m = sys.modules.get(key)
        if m is not None and hasattr(m, 'get_db'):
            return m
    # Fallback: find any loaded module exposing get_db + app
    for m in sys.modules.values():
        if m is not None and hasattr(m, 'get_db') and hasattr(m, 'app'):
            return m
    raise RuntimeError('RecruitOS core (server.py) not loaded')


# ── Database ──────────────────────────────────────────────────────────────
def get_db():
    return _core().get_db()


def ts():
    return _core().ts()


# ── Identity / tenancy ────────────────────────────────────────────────────
def current_user():
    return _core().current_user()


def effective_company_id():
    return _core().effective_company_id()


def real_user_id():
    return _core().real_user_id()


def is_company_admin():
    return _core().is_company_admin()


# ── Auth decorator (reused so module routes behave exactly like core routes) ─
def login_required(fn):
    return _core().login_required(fn)


# ── Universal activity + audit (the shared timeline every module writes to) ─
def log_activity(action, detail='', entity_type='', entity_id=0, meta=None,
                 actor_type='user', actor_name=''):
    return _core().log_activity(action, detail, entity_type, entity_id, meta,
                                actor_type, actor_name)


def log_audit(entity_type, entity_id, field, old_value, new_value,
              actor_type='user', actor_name=''):
    return _core().log_audit(entity_type, entity_id, field, old_value, new_value,
                             actor_type, actor_name)


def record_changes(entity_type, entity_id, before, after, fields,
                   actor_type='user', actor_name=''):
    return _core().record_changes(entity_type, entity_id, before, after, fields,
                                  actor_type, actor_name)


# ── Extra seams used by the scheduler module ───────────────────────────────
def app_secret():
    """The Flask secret key (used to derive unforgeable public link tokens)."""
    return _core().app.secret_key


def platform_smtp_send(to_email, subject, plain_body, html_body=None):
    """Send a system email (booking confirmations etc.) via the core's SMTP."""
    return _core()._platform_smtp_send(to_email, subject, plain_body, html_body)


def log_candidate_event(cid, event_type, detail=''):
    """Write to a candidate's journey timeline."""
    return _core().log_candidate_event(cid, event_type, detail)
