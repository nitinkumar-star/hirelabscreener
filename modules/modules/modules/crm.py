"""
RecruitOS — CRM Module (Layer 2 foundation)

Scope of this PRD:
  • Client Companies (the businesses you recruit FOR)
  • Contacts (the people at those companies — hiring managers, HR, decision makers)
  • Universal timeline + field-level audit on every change
  • Duplicate prevention (no two clients/contacts with the same key identity)
  • Soft delete + restore (nothing is ever hard-deleted)
  • Quick search, advanced filters, sorting, pagination
  • Permissions (tenant-scoped; company-admins can delete/restore)

Architecture:
  • Repository layer  → all SQL lives in *Repo classes (single source of truth)
  • Service layer      → business rules (dedup, validation, audit) live in *Service
  • API layer          → thin Flask routes that call services and shape JSON
This separation means AI/automation features can later call the SAME services
without going through HTTP, and the SQL can be optimised in one place.
"""

import json
import re
from flask import Blueprint, request, jsonify

from modules.shared import (
    get_db, ts, current_user, effective_company_id, real_user_id,
    is_company_admin, login_required, log_activity, record_changes,
)
from modules import register_migration

bp = Blueprint('crm', __name__, url_prefix='/api/crm')


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def migrate(conn):
    """Create CRM tables. Every table is audit-ready:
    created_by / updated_by / created_at / updated_at / is_active (soft delete)."""
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS crm_clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,              -- tenant
        name TEXT NOT NULL,
        industry TEXT DEFAULT '',
        website TEXT DEFAULT '',
        city TEXT DEFAULT '',
        state TEXT DEFAULT '',
        country TEXT DEFAULT 'India',
        gstin TEXT DEFAULT '',
        address TEXT DEFAULT '',
        status TEXT DEFAULT 'active',             -- active | prospect | inactive | lost
        owner_user_id INTEGER DEFAULT 0,          -- assigned recruiter/BD
        notes TEXT DEFAULT '',
        name_key TEXT DEFAULT '',                 -- normalised name for dedup
        is_active INTEGER DEFAULT 1,              -- soft delete flag
        created_by INTEGER DEFAULT 0,
        updated_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS crm_contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,              -- tenant
        client_id INTEGER NOT NULL,               -- FK -> crm_clients.id
        name TEXT NOT NULL,
        designation TEXT DEFAULT '',
        email TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        is_primary INTEGER DEFAULT 0,
        is_decision_maker INTEGER DEFAULT 0,
        linkedin TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        email_key TEXT DEFAULT '',                -- normalised email for dedup
        is_active INTEGER DEFAULT 1,
        created_by INTEGER DEFAULT 0,
        updated_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')

    for sql in [
        'CREATE INDEX IF NOT EXISTS idx_crm_clients_company ON crm_clients(company_id, is_active)',
        'CREATE INDEX IF NOT EXISTS idx_crm_clients_namekey ON crm_clients(company_id, name_key)',
        'CREATE INDEX IF NOT EXISTS idx_crm_clients_status ON crm_clients(company_id, status)',
        'CREATE INDEX IF NOT EXISTS idx_crm_contacts_client ON crm_contacts(client_id, is_active)',
        'CREATE INDEX IF NOT EXISTS idx_crm_contacts_company ON crm_contacts(company_id, is_active)',
        'CREATE INDEX IF NOT EXISTS idx_crm_contacts_emailkey ON crm_contacts(company_id, email_key)',
    ]:
        try:
            c.execute(sql)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════
def _norm_name(s):
    """Normalise a company name for duplicate detection: lowercase, strip
    common suffixes and punctuation so 'Acme Pvt. Ltd.' == 'ACME private limited'."""
    s = (s or '').lower().strip()
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    for suffix in [' private limited', ' pvt ltd', ' pvt limited', ' private ltd',
                   ' limited', ' ltd', ' llp', ' inc', ' corporation', ' corp',
                   ' technologies', ' technology', ' solutions', ' services',
                   ' india', ' pvt', ' co']:
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return re.sub(r'\s+', ' ', s).strip()


def _norm_email(s):
    return (s or '').lower().strip()


VALID_CLIENT_STATUS = {'active', 'prospect', 'inactive', 'lost'}


def _client_public(row):
    d = dict(row)
    d.pop('name_key', None)
    return d


def _contact_public(row):
    d = dict(row)
    d.pop('email_key', None)
    return d


# ══════════════════════════════════════════════════════════════════════════
#  REPOSITORY LAYER  (all SQL lives here)
# ══════════════════════════════════════════════════════════════════════════
class ClientRepo:
    @staticmethod
    def find_by_namekey(conn, company_id, name_key, exclude_id=None):
        q = 'SELECT * FROM crm_clients WHERE company_id=? AND name_key=? AND is_active=1'
        params = [company_id, name_key]
        if exclude_id:
            q += ' AND id!=?'; params.append(exclude_id)
        return conn.execute(q, params).fetchone()

    @staticmethod
    def get(conn, company_id, cid, include_deleted=False):
        q = 'SELECT * FROM crm_clients WHERE id=? AND company_id=?'
        if not include_deleted:
            q += ' AND is_active=1'
        return conn.execute(q, (cid, company_id)).fetchone()

    @staticmethod
    def insert(conn, data):
        cols = ('company_id,name,industry,website,city,state,country,gstin,address,'
                'status,owner_user_id,notes,name_key,is_active,created_by,updated_by,'
                'created_at,updated_at')
        conn.execute(
            f'INSERT INTO crm_clients ({cols}) VALUES ({",".join("?" * 18)})',
            (data['company_id'], data['name'], data['industry'], data['website'],
             data['city'], data['state'], data['country'], data['gstin'],
             data['address'], data['status'], data['owner_user_id'], data['notes'],
             data['name_key'], 1, data['actor'], data['actor'], data['now'], data['now']))
        return conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']

    @staticmethod
    def update(conn, cid, fields, actor, now):
        sets = ', '.join(f'{k}=?' for k in fields) + ', updated_by=?, updated_at=?'
        conn.execute(f'UPDATE crm_clients SET {sets} WHERE id=?',
                     list(fields.values()) + [actor, now, cid])

    @staticmethod
    def soft_delete(conn, cid, actor, now):
        conn.execute('UPDATE crm_clients SET is_active=0, updated_by=?, updated_at=? WHERE id=?',
                     (actor, now, cid))

    @staticmethod
    def restore(conn, cid, actor, now):
        conn.execute('UPDATE crm_clients SET is_active=1, updated_by=?, updated_at=? WHERE id=?',
                     (actor, now, cid))

    @staticmethod
    def search(conn, company_id, filters):
        where = ['company_id=?', 'is_active=?']
        params = [company_id, 0 if filters.get('deleted') else 1]
        if filters.get('q'):
            where.append('(name LIKE ? OR city LIKE ? OR industry LIKE ? OR gstin LIKE ?)')
            like = f'%{filters["q"]}%'; params += [like, like, like, like]
        if filters.get('status'):
            where.append('status=?'); params.append(filters['status'])
        if filters.get('industry'):
            where.append('industry=?'); params.append(filters['industry'])
        if filters.get('city'):
            where.append('city LIKE ?'); params.append(f'%{filters["city"]}%')
        if filters.get('owner_user_id'):
            where.append('owner_user_id=?'); params.append(filters['owner_user_id'])
        where_sql = ' AND '.join(where)

        sort = filters.get('sort', 'created_at')
        if sort not in ('name', 'created_at', 'updated_at', 'status', 'city'):
            sort = 'created_at'
        direction = 'ASC' if filters.get('dir') == 'asc' else 'DESC'

        total = conn.execute(f'SELECT COUNT(*) n FROM crm_clients WHERE {where_sql}', params).fetchone()['n']
        page = max(1, filters.get('page', 1))
        per = min(100, max(1, filters.get('per_page', 25)))
        rows = conn.execute(
            f'SELECT * FROM crm_clients WHERE {where_sql} ORDER BY {sort} {direction} LIMIT ? OFFSET ?',
            params + [per, (page - 1) * per]).fetchall()
        return rows, total, page, per


class ContactRepo:
    @staticmethod
    def find_by_emailkey(conn, company_id, email_key, exclude_id=None):
        if not email_key:
            return None
        q = 'SELECT * FROM crm_contacts WHERE company_id=? AND email_key=? AND is_active=1'
        params = [company_id, email_key]
        if exclude_id:
            q += ' AND id!=?'; params.append(exclude_id)
        return conn.execute(q, params).fetchone()

    @staticmethod
    def get(conn, company_id, cid, include_deleted=False):
        q = 'SELECT * FROM crm_contacts WHERE id=? AND company_id=?'
        if not include_deleted:
            q += ' AND is_active=1'
        return conn.execute(q, (cid, company_id)).fetchone()

    @staticmethod
    def list_for_client(conn, company_id, client_id):
        return conn.execute(
            'SELECT * FROM crm_contacts WHERE company_id=? AND client_id=? AND is_active=1 '
            'ORDER BY is_primary DESC, name ASC', (company_id, client_id)).fetchall()

    @staticmethod
    def insert(conn, data):
        cols = ('company_id,client_id,name,designation,email,phone,is_primary,'
                'is_decision_maker,linkedin,notes,email_key,is_active,created_by,'
                'updated_by,created_at,updated_at')
        conn.execute(
            f'INSERT INTO crm_contacts ({cols}) VALUES ({",".join("?" * 16)})',
            (data['company_id'], data['client_id'], data['name'], data['designation'],
             data['email'], data['phone'], data['is_primary'], data['is_decision_maker'],
             data['linkedin'], data['notes'], data['email_key'], 1, data['actor'],
             data['actor'], data['now'], data['now']))
        return conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']

    @staticmethod
    def update(conn, cid, fields, actor, now):
        sets = ', '.join(f'{k}=?' for k in fields) + ', updated_by=?, updated_at=?'
        conn.execute(f'UPDATE crm_contacts SET {sets} WHERE id=?',
                     list(fields.values()) + [actor, now, cid])

    @staticmethod
    def soft_delete(conn, cid, actor, now):
        conn.execute('UPDATE crm_contacts SET is_active=0, updated_by=?, updated_at=? WHERE id=?',
                     (actor, now, cid))

    @staticmethod
    def clear_primary(conn, client_id, actor, now):
        conn.execute('UPDATE crm_contacts SET is_primary=0, updated_by=?, updated_at=? WHERE client_id=?',
                     (actor, now, client_id))


# ══════════════════════════════════════════════════════════════════════════
#  SERVICE LAYER  (business rules; reusable by API, AI and automation)
# ══════════════════════════════════════════════════════════════════════════
class ValidationError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


class DuplicateError(ValidationError):
    def __init__(self, message, existing_id):
        super().__init__(message, 409)
        self.existing_id = existing_id


AUDIT_CLIENT_FIELDS = ['name', 'industry', 'website', 'city', 'state', 'country',
                       'gstin', 'address', 'status', 'owner_user_id', 'notes']
AUDIT_CONTACT_FIELDS = ['name', 'designation', 'email', 'phone', 'is_primary',
                        'is_decision_maker', 'linkedin', 'notes']


class ClientService:
    @staticmethod
    def create(conn, payload):
        company_id = effective_company_id()
        actor = real_user_id()
        now = ts()
        name = (payload.get('name') or '').strip()
        if not name:
            raise ValidationError('Client name is required.')
        status = (payload.get('status') or 'active').strip().lower()
        if status not in VALID_CLIENT_STATUS:
            raise ValidationError('Invalid status.')

        name_key = _norm_name(name)
        dup = ClientRepo.find_by_namekey(conn, company_id, name_key)
        if dup:
            raise DuplicateError(f'A client named "{dup["name"]}" already exists.', dup['id'])

        data = {
            'company_id': company_id, 'name': name,
            'industry': (payload.get('industry') or '').strip(),
            'website': (payload.get('website') or '').strip(),
            'city': (payload.get('city') or '').strip(),
            'state': (payload.get('state') or '').strip(),
            'country': (payload.get('country') or 'India').strip(),
            'gstin': (payload.get('gstin') or '').strip().upper(),
            'address': (payload.get('address') or '').strip(),
            'status': status,
            'owner_user_id': int(payload.get('owner_user_id') or 0),
            'notes': (payload.get('notes') or '').strip(),
            'name_key': name_key, 'actor': actor, 'now': now,
        }
        cid = ClientRepo.insert(conn, data)
        conn.commit()
        log_activity('client.created', f'Created client "{name}"',
                     entity_type='client', entity_id=cid,
                     meta={'status': status, 'city': data['city']})
        return cid

    @staticmethod
    def update(conn, cid, payload):
        company_id = effective_company_id()
        actor = real_user_id()
        now = ts()
        existing = ClientRepo.get(conn, company_id, cid)
        if not existing:
            raise ValidationError('Client not found.', 404)
        before = dict(existing)

        fields = {}
        if 'name' in payload:
            name = (payload.get('name') or '').strip()
            if not name:
                raise ValidationError('Client name cannot be empty.')
            name_key = _norm_name(name)
            dup = ClientRepo.find_by_namekey(conn, company_id, name_key, exclude_id=cid)
            if dup:
                raise DuplicateError(f'Another client named "{dup["name"]}" already exists.', dup['id'])
            fields['name'] = name
            fields['name_key'] = name_key
        for f in ['industry', 'website', 'city', 'state', 'country', 'address', 'notes']:
            if f in payload:
                fields[f] = (payload.get(f) or '').strip()
        if 'gstin' in payload:
            fields['gstin'] = (payload.get('gstin') or '').strip().upper()
        if 'status' in payload:
            st = (payload.get('status') or '').strip().lower()
            if st not in VALID_CLIENT_STATUS:
                raise ValidationError('Invalid status.')
            fields['status'] = st
        if 'owner_user_id' in payload:
            fields['owner_user_id'] = int(payload.get('owner_user_id') or 0)

        if not fields:
            return
        ClientRepo.update(conn, cid, fields, actor, now)
        conn.commit()
        after = dict(before); after.update(fields)
        changes = record_changes('client', cid, before, after,
                                 [f for f in AUDIT_CLIENT_FIELDS if f in fields])
        log_activity('client.updated', 'Updated client "%s"' % after.get('name', ''),
                     entity_type='client', entity_id=cid,
                     meta={'changes': changes})

    @staticmethod
    def delete(conn, cid):
        company_id = effective_company_id()
        existing = ClientRepo.get(conn, company_id, cid)
        if not existing:
            raise ValidationError('Client not found.', 404)
        ClientRepo.soft_delete(conn, cid, real_user_id(), ts())
        conn.commit()
        log_activity('client.deleted', f'Deleted client "{existing["name"]}"',
                     entity_type='client', entity_id=cid)

    @staticmethod
    def restore(conn, cid):
        company_id = effective_company_id()
        existing = ClientRepo.get(conn, company_id, cid, include_deleted=True)
        if not existing:
            raise ValidationError('Client not found.', 404)
        ClientRepo.restore(conn, cid, real_user_id(), ts())
        conn.commit()
        log_activity('client.restored', f'Restored client "{existing["name"]}"',
                     entity_type='client', entity_id=cid)


class ContactService:
    @staticmethod
    def create(conn, client_id, payload):
        company_id = effective_company_id()
        actor = real_user_id()
        now = ts()
        client = ClientRepo.get(conn, company_id, client_id)
        if not client:
            raise ValidationError('Client not found.', 404)
        name = (payload.get('name') or '').strip()
        if not name:
            raise ValidationError('Contact name is required.')
        email = _norm_email(payload.get('email'))
        if email:
            dup = ContactRepo.find_by_emailkey(conn, company_id, email)
            if dup:
                raise DuplicateError(f'A contact with email {email} already exists.', dup['id'])

        is_primary = 1 if payload.get('is_primary') else 0
        if is_primary:
            ContactRepo.clear_primary(conn, client_id, actor, now)

        data = {
            'company_id': company_id, 'client_id': client_id, 'name': name,
            'designation': (payload.get('designation') or '').strip(),
            'email': (payload.get('email') or '').strip(),
            'phone': (payload.get('phone') or '').strip(),
            'is_primary': is_primary,
            'is_decision_maker': 1 if payload.get('is_decision_maker') else 0,
            'linkedin': (payload.get('linkedin') or '').strip(),
            'notes': (payload.get('notes') or '').strip(),
            'email_key': email, 'actor': actor, 'now': now,
        }
        cid = ContactRepo.insert(conn, data)
        conn.commit()
        log_activity('contact.created', f'Added contact "{name}" to "{client["name"]}"',
                     entity_type='client', entity_id=client_id,
                     meta={'contact_id': cid})
        return cid

    @staticmethod
    def update(conn, contact_id, payload):
        company_id = effective_company_id()
        actor = real_user_id()
        now = ts()
        existing = ContactRepo.get(conn, company_id, contact_id)
        if not existing:
            raise ValidationError('Contact not found.', 404)
        before = dict(existing)

        fields = {}
        if 'name' in payload:
            nm = (payload.get('name') or '').strip()
            if not nm:
                raise ValidationError('Contact name cannot be empty.')
            fields['name'] = nm
        if 'email' in payload:
            email_key = _norm_email(payload.get('email'))
            if email_key:
                dup = ContactRepo.find_by_emailkey(conn, company_id, email_key, exclude_id=contact_id)
                if dup:
                    raise DuplicateError(f'Another contact uses email {email_key}.', dup['id'])
            fields['email'] = (payload.get('email') or '').strip()
            fields['email_key'] = email_key
        for f in ['designation', 'phone', 'linkedin', 'notes']:
            if f in payload:
                fields[f] = (payload.get(f) or '').strip()
        if 'is_decision_maker' in payload:
            fields['is_decision_maker'] = 1 if payload.get('is_decision_maker') else 0
        if 'is_primary' in payload:
            ip = 1 if payload.get('is_primary') else 0
            if ip:
                ContactRepo.clear_primary(conn, existing['client_id'], actor, now)
            fields['is_primary'] = ip

        if not fields:
            return
        ContactRepo.update(conn, contact_id, fields, actor, now)
        conn.commit()
        after = dict(before); after.update(fields)
        changes = record_changes('contact', contact_id, before, after,
                                 [f for f in AUDIT_CONTACT_FIELDS if f in fields])
        log_activity('contact.updated', f'Updated contact "{after.get("name", "")}"',
                     entity_type='client', entity_id=existing['client_id'],
                     meta={'contact_id': contact_id, 'changes': changes})

    @staticmethod
    def delete(conn, contact_id):
        company_id = effective_company_id()
        existing = ContactRepo.get(conn, company_id, contact_id)
        if not existing:
            raise ValidationError('Contact not found.', 404)
        ContactRepo.soft_delete(conn, contact_id, real_user_id(), ts())
        conn.commit()
        log_activity('contact.deleted', f'Deleted contact "{existing["name"]}"',
                     entity_type='client', entity_id=existing['client_id'],
                     meta={'contact_id': contact_id})


# ══════════════════════════════════════════════════════════════════════════
#  API LAYER
# ══════════════════════════════════════════════════════════════════════════
def _err(e):
    return jsonify({'error': e.message, **({'existing_id': e.existing_id} if isinstance(e, DuplicateError) else {})}), e.code


@bp.route('/clients', methods=['GET'])
@login_required
def list_clients():
    company_id = effective_company_id()
    filters = {
        'q': request.args.get('q', '').strip(),
        'status': request.args.get('status', '').strip().lower(),
        'industry': request.args.get('industry', '').strip(),
        'city': request.args.get('city', '').strip(),
        'owner_user_id': request.args.get('owner_user_id', type=int),
        'sort': request.args.get('sort', 'created_at'),
        'dir': request.args.get('dir', 'desc'),
        'page': request.args.get('page', 1, type=int),
        'per_page': request.args.get('per_page', 25, type=int),
        'deleted': request.args.get('deleted') == '1',
    }
    conn = get_db()
    rows, total, page, per = ClientRepo.search(conn, company_id, filters)
    # attach contact counts
    out = []
    for r in rows:
        d = _client_public(r)
        d['contact_count'] = conn.execute(
            'SELECT COUNT(*) n FROM crm_contacts WHERE client_id=? AND is_active=1', (r['id'],)).fetchone()['n']
        out.append(d)
    conn.close()
    return jsonify({'ok': True, 'clients': out, 'total': total, 'page': page,
                    'per_page': per, 'pages': (total + per - 1) // per})


@bp.route('/clients', methods=['POST'])
@login_required
def create_client():
    conn = get_db()
    try:
        cid = ClientService.create(conn, request.json or {})
        row = ClientRepo.get(conn, effective_company_id(), cid)
        conn.close()
        return jsonify({'ok': True, 'client': _client_public(row)})
    except ValidationError as e:
        conn.close(); return _err(e)


@bp.route('/clients/<int:cid>', methods=['GET'])
@login_required
def get_client(cid):
    conn = get_db()
    row = ClientRepo.get(conn, effective_company_id(), cid)
    if not row:
        conn.close(); return jsonify({'error': 'Client not found.'}), 404
    contacts = ContactRepo.list_for_client(conn, effective_company_id(), cid)
    conn.close()
    return jsonify({'ok': True, 'client': _client_public(row),
                    'contacts': [_contact_public(c) for c in contacts]})


@bp.route('/clients/<int:cid>', methods=['PUT'])
@login_required
def update_client(cid):
    conn = get_db()
    try:
        ClientService.update(conn, cid, request.json or {})
        row = ClientRepo.get(conn, effective_company_id(), cid)
        conn.close()
        return jsonify({'ok': True, 'client': _client_public(row)})
    except ValidationError as e:
        conn.close(); return _err(e)


@bp.route('/clients/<int:cid>', methods=['DELETE'])
@login_required
def delete_client(cid):
    if not is_company_admin():
        return jsonify({'error': 'Only a company admin can delete clients.'}), 403
    conn = get_db()
    try:
        ClientService.delete(conn, cid)
        conn.close()
        return jsonify({'ok': True})
    except ValidationError as e:
        conn.close(); return _err(e)


@bp.route('/clients/<int:cid>/restore', methods=['POST'])
@login_required
def restore_client(cid):
    if not is_company_admin():
        return jsonify({'error': 'Only a company admin can restore clients.'}), 403
    conn = get_db()
    try:
        ClientService.restore(conn, cid)
        conn.close()
        return jsonify({'ok': True})
    except ValidationError as e:
        conn.close(); return _err(e)


@bp.route('/clients/<int:cid>/contacts', methods=['POST'])
@login_required
def add_contact(cid):
    conn = get_db()
    try:
        contact_id = ContactService.create(conn, cid, request.json or {})
        row = ContactRepo.get(conn, effective_company_id(), contact_id)
        conn.close()
        return jsonify({'ok': True, 'contact': _contact_public(row)})
    except ValidationError as e:
        conn.close(); return _err(e)


@bp.route('/contacts/<int:contact_id>', methods=['PUT'])
@login_required
def update_contact(contact_id):
    conn = get_db()
    try:
        ContactService.update(conn, contact_id, request.json or {})
        row = ContactRepo.get(conn, effective_company_id(), contact_id)
        conn.close()
        return jsonify({'ok': True, 'contact': _contact_public(row)})
    except ValidationError as e:
        conn.close(); return _err(e)


@bp.route('/contacts/<int:contact_id>', methods=['DELETE'])
@login_required
def delete_contact(contact_id):
    conn = get_db()
    try:
        ContactService.delete(conn, contact_id)
        conn.close()
        return jsonify({'ok': True})
    except ValidationError as e:
        conn.close(); return _err(e)


@bp.route('/meta', methods=['GET'])
@login_required
def crm_meta():
    """Distinct industries/cities for filter dropdowns + status list."""
    company_id = effective_company_id()
    conn = get_db()
    industries = [r['industry'] for r in conn.execute(
        "SELECT DISTINCT industry FROM crm_clients WHERE company_id=? AND is_active=1 AND industry!='' ORDER BY industry",
        (company_id,)).fetchall()]
    cities = [r['city'] for r in conn.execute(
        "SELECT DISTINCT city FROM crm_clients WHERE company_id=? AND is_active=1 AND city!='' ORDER BY city",
        (company_id,)).fetchall()]
    conn.close()
    return jsonify({'ok': True, 'industries': industries, 'cities': cities,
                    'statuses': sorted(VALID_CLIENT_STATUS)})
