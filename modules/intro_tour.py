"""
RecruitOS — Intro tour for new users  (Nov 2026)

The first time a person signs in, the app walks through every tab of the
sidebar: it opens each tab and explains what it is for (index.html, startTour).
Finished or skipped is remembered on the user (users.tour_done_at), so it does
not repeat on another computer. "Take the tour" in Settings → My Account plays
it again.

  POST /api/me/tour   {done: true|false}   remember / forget that the tour was seen
"""

import datetime

from flask import Blueprint, request, jsonify, session

from modules.shared import get_db, ts, login_required
from modules import register_migration

bp = Blueprint('intro_tour', __name__)


@register_migration
def migrate(conn):
    try:
        conn.execute("ALTER TABLE users ADD COLUMN tour_done_at TEXT DEFAULT ''")
        # People who already use the app are not interrupted: only users created
        # from now on get the tour on their first sign-in.
        conn.execute("UPDATE users SET tour_done_at=?", (datetime.datetime.now().isoformat(timespec='seconds'),))
    except Exception:
        pass
    conn.commit()


def tour_done(uid):
    conn = get_db()
    try:
        r = conn.execute('SELECT tour_done_at FROM users WHERE id=?', (uid,)).fetchone()
        return bool(r and r['tour_done_at'])
    except Exception:
        return True                     # column missing: never force the tour
    finally:
        conn.close()


@bp.route('/api/me/tour', methods=['POST'])
@login_required
def save_tour():
    done = (request.get_json(silent=True) or {}).get('done', True)
    conn = get_db()
    try:
        conn.execute('UPDATE users SET tour_done_at=? WHERE id=?', (ts() if done else '', session.get('user_id')))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})
