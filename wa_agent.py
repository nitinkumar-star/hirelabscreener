"""
RecruitOS — WhatsApp AI Agent Module

The autonomous recruitment conversation engine. When a candidate is added to the
ATS, the system sends a WhatsApp intro. When the candidate replies, DeepSeek AI
classifies the intent and either:
  • Acts autonomously (share JD, answer known questions, schedule call)
  • Escalates to the recruiter (unknown question → recruiter answers → AI learns)

Over time the AI handles 80%+ of conversations independently, trained by the
recruiter's own domain knowledge.

Architecture:
  • wa_conversations    — per-candidate WhatsApp conversation thread
  • wa_messages         — every message (sent/received) with metadata
  • wa_learned_responses — the AI's growing knowledge base (recruiter-taught)
  • WhatsApp Cloud API  — Meta's direct API (cheapest, no middleman)
  • DeepSeek            — intent classification + response generation
"""

import json
import re
import datetime
from flask import Blueprint, request, jsonify

from modules.shared import (
    get_db, ts, current_user, effective_company_id, real_user_id,
    is_company_admin, login_required, log_activity,
)
from modules import register_migration

bp = Blueprint('wa_agent', __name__, url_prefix='/api/wa')


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def migrate(conn):
    c = conn.cursor()

    # Conversation thread per candidate (one thread per candidate per mandate)
    c.execute('''CREATE TABLE IF NOT EXISTS wa_conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL,
        mandate_id INTEGER,
        candidate_phone TEXT DEFAULT '',
        candidate_name TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        wa_thread_id TEXT DEFAULT '',
        auto_mode INTEGER DEFAULT 1,
        escalated INTEGER DEFAULT 0,
        escalation_reason TEXT DEFAULT '',
        last_message_at TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')

    # Every message in both directions
    c.execute('''CREATE TABLE IF NOT EXISTS wa_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL,
        direction TEXT DEFAULT 'outbound',
        sender TEXT DEFAULT '',
        content TEXT DEFAULT '',
        message_type TEXT DEFAULT 'text',
        wa_message_id TEXT DEFAULT '',
        ai_intent TEXT DEFAULT '',
        ai_confidence REAL DEFAULT 0,
        ai_action_taken TEXT DEFAULT '',
        delivered INTEGER DEFAULT 0,
        read_at TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )''')

    # The AI's growing knowledge base — recruiter teaches, AI remembers
    c.execute('''CREATE TABLE IF NOT EXISTS wa_learned_responses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        intent_pattern TEXT DEFAULT '',
        sample_question TEXT DEFAULT '',
        response_template TEXT DEFAULT '',
        action TEXT DEFAULT '',
        taught_by INTEGER DEFAULT 0,
        use_count INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')

    # Pending escalations waiting for recruiter response
    c.execute('''CREATE TABLE IF NOT EXISTS wa_escalations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        conversation_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        candidate_question TEXT DEFAULT '',
        ai_suggested_response TEXT DEFAULT '',
        recruiter_response TEXT DEFAULT '',
        learn_as_pattern INTEGER DEFAULT 0,
        status TEXT DEFAULT 'pending',
        resolved_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        resolved_at TEXT DEFAULT ''
    )''')

    for idx in [
        'CREATE INDEX IF NOT EXISTS idx_wa_conv_candidate ON wa_conversations(candidate_id)',
        'CREATE INDEX IF NOT EXISTS idx_wa_conv_company ON wa_conversations(company_id, status)',
        'CREATE INDEX IF NOT EXISTS idx_wa_msg_conv ON wa_messages(conversation_id)',
        'CREATE INDEX IF NOT EXISTS idx_wa_learned_company ON wa_learned_responses(company_id, is_active)',
        'CREATE INDEX IF NOT EXISTS idx_wa_esc_company ON wa_escalations(company_id, status)',
    ]:
        try:
            c.execute(idx)
        except Exception:
            pass

    # Additive: qualification state machine + failure tracking (Rule 0.3)
    for col, defn in [
        ('qualification_state', "TEXT DEFAULT ''"),     # JSON: step, collected, retries
        ('failure_reason', "TEXT DEFAULT ''"),          # invalid_number / send_failed
    ]:
        try:
            c.execute(f'ALTER TABLE wa_conversations ADD COLUMN {col} {defn}')
        except Exception:
            pass

    # Seed default learned responses (common patterns every recruiter faces)
    existing = conn.execute('SELECT COUNT(*) n FROM wa_learned_responses').fetchone()['n']
    if existing == 0:
        seeds = [
            ('jd_request', 'Send me the JD', 'Sure! I am sharing the Job Description on your email and WhatsApp right away.',
             'share_jd'),
            ('company_ask', 'Which company is this for?', '',  # empty = escalate
             'escalate'),
            ('salary_ask', 'What is the salary/CTC?', 'The compensation details will be discussed during our conversation. When would be a good time to talk?',
             'deflect_ctc'),
            ('not_interested', 'Not interested', 'No problem at all. Thank you for your time. We will keep you in mind for future opportunities that match your profile.',
             'mark_not_interested'),
            ('interested', 'Yes I am interested', 'That is great to hear! When would be a convenient time for a quick call to discuss the details?',
             'mark_interested'),
            ('call_time', 'Call me at 3pm', 'Noted! I will call you today. Thank you.',
             'schedule_call'),
            ('location_ask', 'Where is the job location?', '',
             'answer_from_mandate'),
            ('notice_share', 'My notice period is 30 days', 'Thank you for sharing. I have noted your notice period.',
             'update_notice'),
            ('current_ctc_share', 'My current CTC is 12 LPA', 'Thank you for sharing your current compensation details.',
             'update_ctc'),
        ]
        for intent, sample, response, action in seeds:
            conn.execute(
                'INSERT INTO wa_learned_responses (company_id,intent_pattern,sample_question,'
                'response_template,action,taught_by,is_active,created_at,updated_at) '
                'VALUES (0,?,?,?,?,0,1,?,?)',
                (intent, sample, response, action, ts(), ts()))


# ══════════════════════════════════════════════════════════════════════════
#  WHATSAPP CLOUD API — send/receive layer
# ══════════════════════════════════════════════════════════════════════════
def _wa_config():
    """Read WhatsApp Cloud API credentials from tenant settings or env vars."""
    import os
    return {
        'token': os.environ.get('WA_ACCESS_TOKEN', '').strip(),
        'phone_id': os.environ.get('WA_PHONE_NUMBER_ID', '').strip(),
        'verify_token': os.environ.get('WA_VERIFY_TOKEN', 'hirelab_wa_verify').strip(),
    }


def wa_send_text(to_phone, text):
    """Send a plain text WhatsApp message via Meta Cloud API."""
    import requests as _req
    cfg = _wa_config()
    if not cfg['token'] or not cfg['phone_id']:
        return False, 'WhatsApp API not configured. Set WA_ACCESS_TOKEN and WA_PHONE_NUMBER_ID.'
    # Normalize Indian number
    phone = re.sub(r'[^0-9]', '', to_phone)
    if len(phone) == 10:
        phone = '91' + phone
    try:
        resp = _req.post(
            f'https://graph.facebook.com/v18.0/{cfg["phone_id"]}/messages',
            headers={'Authorization': f'Bearer {cfg["token"]}', 'Content-Type': 'application/json'},
            json={
                'messaging_product': 'whatsapp',
                'to': phone,
                'type': 'text',
                'text': {'body': text},
            },
            timeout=15)
        data = resp.json()
        msg_id = ''
        if 'messages' in data and data['messages']:
            msg_id = data['messages'][0].get('id', '')
        if resp.status_code in (200, 201):
            return True, msg_id
        return False, data.get('error', {}).get('message', str(data))
    except Exception as e:
        return False, str(e)


def wa_send_document(to_phone, document_url, filename, caption=''):
    """Send a document (JD PDF/link) via WhatsApp."""
    import requests as _req
    cfg = _wa_config()
    if not cfg['token'] or not cfg['phone_id']:
        return False, 'WhatsApp API not configured.'
    phone = re.sub(r'[^0-9]', '', to_phone)
    if len(phone) == 10:
        phone = '91' + phone
    try:
        resp = _req.post(
            f'https://graph.facebook.com/v18.0/{cfg["phone_id"]}/messages',
            headers={'Authorization': f'Bearer {cfg["token"]}', 'Content-Type': 'application/json'},
            json={
                'messaging_product': 'whatsapp',
                'to': phone,
                'type': 'document',
                'document': {'link': document_url, 'filename': filename, 'caption': caption},
            },
            timeout=15)
        return resp.status_code in (200, 201), resp.text
    except Exception as e:
        return False, str(e)


# ══════════════════════════════════════════════════════════════════════════
#  AI INTENT ENGINE — DeepSeek classifies candidate messages
# ══════════════════════════════════════════════════════════════════════════
INTENT_CLASSIFICATION_PROMPT = """You are an AI assistant for an Indian recruitment agency. A candidate has replied to a WhatsApp message about a job opportunity.

Analyze the candidate's message and classify their intent. Return ONLY a JSON object:

{
  "intent": "<one of: jd_request, company_ask, salary_ask, location_ask, not_interested, interested, call_time, notice_share, current_ctc_share, question, greeting, acknowledgment, unclear>",
  "confidence": <0.0 to 1.0>,
  "extracted_data": {
    "call_time": "<if they mentioned a time, e.g. '3pm today'>",
    "notice_period": <if they shared notice in days, e.g. 30>,
    "ctc": <if they shared CTC in LPA, e.g. 12.5>,
    "location_preference": "<if mentioned>"
  },
  "summary": "<one line summary of what the candidate said>"
}

Context about the job:
Role: {role}
Location: {location}

Candidate's message:
"{message}"

Return ONLY valid JSON, no markdown, no explanation."""


def classify_intent(message, role='', location='', deepseek_key=''):
    """Use DeepSeek to classify a candidate's WhatsApp reply."""
    if not deepseek_key:
        return {'intent': 'unclear', 'confidence': 0, 'extracted_data': {}, 'summary': message[:100]}

    import requests as _req
    prompt = INTENT_CLASSIFICATION_PROMPT.format(
        role=role or 'Not specified', location=location or 'Not specified', message=message)
    try:
        resp = _req.post('https://api.deepseek.com/chat/completions',
            headers={'Authorization': f'Bearer {deepseek_key}', 'Content-Type': 'application/json'},
            json={'model': 'deepseek-chat', 'messages': [{'role': 'user', 'content': prompt}],
                  'temperature': 0.1, 'max_tokens': 500},
            timeout=30)
        text = resp.json().get('choices', [{}])[0].get('message', {}).get('content', '{}')
        # Parse JSON from response
        text = text.strip()
        if '```' in text:
            for part in text.split('```'):
                p = part.strip()
                if p.startswith('json'):
                    p = p[4:].strip()
                if p.startswith('{'):
                    text = p
                    break
        result = json.loads(text)
        return result
    except Exception as e:
        print(f'[wa_agent] classify_intent error: {e}')
        return {'intent': 'unclear', 'confidence': 0, 'extracted_data': {}, 'summary': message[:100]}


GENERATE_RESPONSE_PROMPT = """You are a professional recruitment consultant from an Indian agency. A candidate asked a question about a job opportunity. Generate a short, professional WhatsApp reply.

Job details:
Role: {role}
Client: {client}
Location: {location}
JD Summary: {jd_summary}

Candidate's question: "{question}"

Rules:
- Keep it under 50 words
- Professional but warm
- NEVER mention salary/CTC/compensation numbers
- NEVER share client name unless explicitly told to
- If you don't know the answer, say "Let me check with my team and get back to you shortly."
- Reply in the same language the candidate used (Hindi/English)

Return ONLY the reply text, nothing else."""


def generate_response(question, role='', client='', location='', jd_summary='', deepseek_key=''):
    """Generate a contextual WhatsApp reply for a candidate question."""
    if not deepseek_key:
        return ''
    import requests as _req
    prompt = GENERATE_RESPONSE_PROMPT.format(
        role=role, client=client or '[Confidential]', location=location,
        jd_summary=jd_summary[:500] if jd_summary else 'Not available', question=question)
    # Personal style: make the AI write like this recruiter (learned via
    # "Learn My Style" in Settings). Prepended so it shapes the whole reply.
    _style = _get_setting('wa_style_profile', '')
    if _style:
        prompt = ("Write the reply EXACTLY in my personal WhatsApp style:\n"
                  + _style + "\n\n") + prompt
    try:
        resp = _req.post('https://api.deepseek.com/chat/completions',
            headers={'Authorization': f'Bearer {deepseek_key}', 'Content-Type': 'application/json'},
            json={'model': 'deepseek-chat', 'messages': [{'role': 'user', 'content': prompt}],
                  'temperature': 0.3, 'max_tokens': 200},
            timeout=30)
        return resp.json().get('choices', [{}])[0].get('message', {}).get('content', '').strip()
    except Exception:
        return ''


# ══════════════════════════════════════════════════════════════════════════
#  CONVERSATION ENGINE — the orchestrator
# ══════════════════════════════════════════════════════════════════════════
def _get_setting(key, default=''):
    """Read a tenant setting (bridges to server.py's get_setting)."""
    try:
        from modules.shared import _core
        return _core().get_setting(key, default)
    except Exception:
        return default


def _strip_ctc_from_jd(jd_text):
    """Remove any CTC/salary/compensation mentions from JD text."""
    if not jd_text:
        return ''
    lines = jd_text.split('\n')
    filtered = []
    for line in lines:
        lower = line.lower()
        if any(kw in lower for kw in ['ctc', 'salary', 'compensation', 'package', 'lpa', 'lakh',
                                       'stipend', 'remuneration', 'pay range', 'budget']):
            continue
        filtered.append(line)
    return '\n'.join(filtered)


def start_conversation(conn, candidate_id, mandate_id):
    """Send the initial WhatsApp intro message when a candidate is added.
    Returns (success, error_or_conversation_id)."""
    company_id = effective_company_id()

    # Get candidate details
    c = conn.execute('SELECT name, phone, email FROM candidates WHERE id=?', (candidate_id,)).fetchone()
    if not c:
        return False, 'Candidate not found'

    # Rule 0.3: Invalid or missing WhatsApp number handling
    phone_raw = (c['phone'] or '').strip()
    phone_digits = re.sub(r'[^0-9]', '', phone_raw)
    invalid_reason = ''
    if not phone_raw:
        invalid_reason = 'No phone number on candidate profile'
    elif len(phone_digits) < 10:
        invalid_reason = f'Phone number "{phone_raw}" is too short to be a valid WhatsApp number'
    elif len(phone_digits) > 13:
        invalid_reason = f'Phone number "{phone_raw}" has too many digits'
    if invalid_reason:
        # Record a "failed" conversation so recruiter sees it in the dashboard
        conn.execute(
            'INSERT INTO wa_conversations (company_id,candidate_id,mandate_id,candidate_phone,'
            'candidate_name,status,failure_reason,last_message_at,created_at,updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (company_id, candidate_id, mandate_id, phone_raw, c['name'] or '',
             'failed_invalid_number', invalid_reason, ts(), ts(), ts()))
        conn.commit()
        log_activity('wa.intro_failed', f'WhatsApp intro failed: {invalid_reason}',
                     entity_type='candidate', entity_id=candidate_id,
                     meta={'reason': 'invalid_number', 'phone': phone_raw})
        return False, invalid_reason

    # Get mandate details
    m = conn.execute('SELECT role, client, location FROM mandates WHERE id=?', (mandate_id,)).fetchone()
    if not m:
        return False, 'Mandate not found'

    # Get recruiter name
    u = current_user()
    recruiter_name = ''
    if u:
        # Try profile display name first
        profile = conn.execute('SELECT display_name FROM users WHERE id=?', (u['id'],)).fetchone()
        recruiter_name = (profile['display_name'] if profile and profile['display_name'] else u.get('username', ''))
    company_name = _get_setting('company_name', 'our recruitment team')

    # Check if conversation already exists
    existing = conn.execute(
        'SELECT id FROM wa_conversations WHERE candidate_id=? AND mandate_id=? AND company_id=?',
        (candidate_id, mandate_id, company_id)).fetchone()
    if existing:
        return True, existing['id']  # already started

    # Build the intro message
    intro = (f"Hi {c['name'] or 'there'},\n\n"
             f"I am {recruiter_name} from {company_name}.\n\n"
             f"We have an exciting opportunity for *{m['role']}*"
             + (f" in *{m['location']}*" if m['location'] else '') + ".\n\n"
             f"When would be a good time for a quick call to discuss this further?\n\n"
             f"Looking forward to hearing from you.")

    # Send via WhatsApp
    ok, result = wa_send_text(c['phone'], intro)

    # Create conversation record (even if send failed — we'll retry)
    conv_id = conn.execute(
        'INSERT INTO wa_conversations (company_id,candidate_id,mandate_id,candidate_phone,'
        'candidate_name,status,wa_thread_id,auto_mode,last_message_at,created_at,updated_at) '
        'VALUES (?,?,?,?,?,?,?,1,?,?,?)',
        (company_id, candidate_id, mandate_id, c['phone'], c['name'] or '',
         'active', '', ts(), ts(), ts())
    ).lastrowid

    # Log the outbound message
    conn.execute(
        'INSERT INTO wa_messages (conversation_id,direction,sender,content,message_type,'
        'wa_message_id,delivered,created_at) VALUES (?,?,?,?,?,?,?,?)',
        (conv_id, 'outbound', recruiter_name, intro, 'text',
         result if ok else '', 1 if ok else 0, ts()))

    conn.commit()

    log_activity('wa.intro_sent', f'WhatsApp intro sent to {c["name"]} for {m["role"]}',
                 entity_type='candidate', entity_id=candidate_id,
                 meta={'mandate_id': mandate_id, 'wa_sent': ok})

    if ok:
        return True, conv_id
    else:
        return False, f'Message saved but WhatsApp send failed: {result}'


def process_incoming_message(conn, phone, message_text, wa_message_id=''):
    """Process an incoming WhatsApp message from a candidate.
    This is the brain — classifies intent, finds/creates conversation,
    executes actions or escalates to recruiter."""

    # Find the conversation by phone number
    phone_clean = re.sub(r'[^0-9]', '', phone)
    if phone_clean.startswith('91') and len(phone_clean) == 12:
        phone_10 = phone_clean[2:]
    else:
        phone_10 = phone_clean

    conv = conn.execute(
        "SELECT c.*, m.role, m.client, m.location, m.jd FROM wa_conversations c "
        "LEFT JOIN mandates m ON m.id=c.mandate_id "
        "WHERE (c.candidate_phone LIKE ? OR c.candidate_phone LIKE ?) AND c.status='active' "
        "ORDER BY c.last_message_at DESC LIMIT 1",
        (f'%{phone_10}', f'%{phone_clean}')).fetchone()

    if not conv:
        return None  # Unknown number — no active conversation

    conv_id = conv['id']
    company_id = conv['company_id']

    # Save the incoming message
    msg_id = conn.execute(
        'INSERT INTO wa_messages (conversation_id,direction,sender,content,message_type,'
        'wa_message_id,created_at) VALUES (?,?,?,?,?,?,?)',
        (conv_id, 'inbound', conv['candidate_name'], message_text, 'text',
         wa_message_id, ts())).lastrowid

    conn.execute('UPDATE wa_conversations SET last_message_at=?, updated_at=? WHERE id=?',
                 (ts(), ts(), conv_id))
    conn.commit()

    # ── Qualification flow interception (Rules Q.1 - Q.7) ────────────────
    # If a qualification is in progress on this conversation, treat this
    # message as an answer to the pending question rather than a fresh
    # intent classification. STOP/not-interested still work below.
    low_txt = (message_text or '').strip().lower()
    is_stop_signal = any(w in low_txt for w in ['stop', 'unsubscribe', 'not interested',
                                                  'no thanks', 'nahi chahiye'])
    if not is_stop_signal:
        # Re-read the conversation to pick up qualification_state
        conv_full = conn.execute('SELECT * FROM wa_conversations WHERE id=?',
                                  (conv_id,)).fetchone()
        if conv_full and _parse_qual_state(conv_full).get('step'):
            handled = _process_qualification_reply(conn, conv_full, msg_id, message_text)
            if handled:
                return {'action': 'qualification', 'handled_by_flow': True}

    # Get DeepSeek key
    deepseek_key = _get_setting('deepseek_api_key', '')
    if not deepseek_key:
        import os
        deepseek_key = os.environ.get('DEEPSEEK_API_KEY', '')

    # Classify intent
    classification = classify_intent(
        message_text, role=conv['role'] or '', location=conv['location'] or '',
        deepseek_key=deepseek_key)

    intent = classification.get('intent', 'unclear')
    confidence = classification.get('confidence', 0)
    extracted = classification.get('extracted_data', {})

    # Update message with AI analysis
    conn.execute('UPDATE wa_messages SET ai_intent=?, ai_confidence=? WHERE id=?',
                 (intent, confidence, msg_id))

    # Check learned responses first (recruiter-taught patterns)
    learned = conn.execute(
        'SELECT * FROM wa_learned_responses WHERE (company_id=? OR company_id=0) '
        'AND intent_pattern=? AND is_active=1 ORDER BY company_id DESC LIMIT 1',
        (company_id, intent)).fetchone()

    action = None
    response = None

    if learned:
        action = learned['action']
        response = learned['response_template']
        # Increment use count
        conn.execute('UPDATE wa_learned_responses SET use_count=use_count+1, updated_at=? WHERE id=?',
                     (ts(), learned['id']))

    # Execute action based on intent
    result = _execute_action(conn, conv, msg_id, intent, action, response,
                             message_text, classification, deepseek_key)

    conn.commit()
    return result


def _execute_action(conn, conv, msg_id, intent, action, response, candidate_msg,
                    classification, deepseek_key):
    """Execute the appropriate action for a classified intent."""

    conv_id = conv['id']
    candidate_id = conv['candidate_id']
    mandate_id = conv['mandate_id']

    # ── AGENTIC: Share JD (without CTC) ──
    if action == 'share_jd':
        jd_text = _strip_ctc_from_jd(conv['jd'] or '')
        if jd_text.strip():
            # Send JD on WhatsApp
            jd_msg = f"Here is the Job Description for *{conv['role']}*:\n\n{jd_text}"
            ok, _ = wa_send_text(conv['candidate_phone'], response or 'Sharing the JD now.')
            if jd_msg:
                wa_send_text(conv['candidate_phone'], jd_msg)
            # Also send via email if we have it
            _send_jd_email(conn, candidate_id, conv['role'] or '', jd_text)
            _log_outbound(conn, conv_id, response or 'Sharing the JD.', 'share_jd')
            _log_outbound(conn, conv_id, f'[JD shared for {conv["role"]}]', 'share_jd')
        else:
            _escalate(conn, conv, msg_id, candidate_msg,
                      'Candidate asked for JD but no JD is available for this mandate.')
        return {'action': 'share_jd', 'auto': True}

    # ── AGENTIC: Mark not interested → update stage ──
    if action == 'mark_not_interested':
        wa_send_text(conv['candidate_phone'], response)
        _log_outbound(conn, conv_id, response, 'mark_not_interested')
        conn.execute("UPDATE candidates SET stage='Not Interested', updated_at=? WHERE id=?",
                     (ts(), candidate_id))
        # Cancel any active qualification flow
        conn.execute('UPDATE wa_conversations SET qualification_state=?, status=?, updated_at=? WHERE id=?',
                     ('', 'closed', ts(), conv_id))
        # Cancel any pending follow-up reminders for this candidate
        try:
            conn.execute("UPDATE reminders SET done=1 WHERE candidate_id=? AND done=0",
                         (candidate_id,))
        except Exception:
            pass
        # Try to extract a reason from the message
        reason = ''
        try:
            summary = classification.get('summary', '') if isinstance(classification, dict) else ''
            if summary and len(summary) < 200:
                reason = summary
        except Exception:
            pass
        detail = f'{conv["candidate_name"]} marked not interested via WhatsApp'
        if reason:
            detail += f' \u2014 reason: {reason}'
        log_activity('wa.not_interested', detail,
                     entity_type='candidate', entity_id=candidate_id,
                     meta={'reason': reason, 'raw_message': candidate_msg[:200]})
        return {'action': 'mark_not_interested', 'auto': True, 'reason': reason}

    # ── AGENTIC: Mark interested → update stage ──
    if action == 'mark_interested':
        wa_send_text(conv['candidate_phone'], response)
        _log_outbound(conn, conv_id, response, 'mark_interested')
        conn.execute("UPDATE candidates SET stage='Interested', updated_at=? WHERE id=?",
                     (ts(), candidate_id))
        log_activity('wa.interested', f'{conv["candidate_name"]} is interested via WhatsApp',
                     entity_type='candidate', entity_id=candidate_id)
        return {'action': 'mark_interested', 'auto': True}

    # ── Answer from mandate data (location, etc.) ──
    if action == 'answer_from_mandate':
        answer = ''
        if intent == 'location_ask' and conv['location']:
            answer = f"The job is located in *{conv['location']}*."
        if answer:
            wa_send_text(conv['candidate_phone'], answer)
            _log_outbound(conn, conv_id, answer, 'answer_from_mandate')
            return {'action': 'answer_from_mandate', 'auto': True}
        else:
            _escalate(conn, conv, msg_id, candidate_msg,
                      f'Candidate asked about {intent} but data not available in mandate.')
            return {'action': 'escalated', 'auto': False}

    # ── Deflect CTC (trained response, never share numbers) ──
    if action == 'deflect_ctc' and response:
        wa_send_text(conv['candidate_phone'], response)
        _log_outbound(conn, conv_id, response, 'deflect_ctc')
        return {'action': 'deflect_ctc', 'auto': True}

    # ── Extract and update candidate data (notice period, CTC shared by candidate) ──
    if action == 'update_notice':
        notice = classification.get('extracted_data', {}).get('notice_period')
        if notice:
            conn.execute('UPDATE candidates SET notice_period=?, updated_at=? WHERE id=?',
                         (notice, ts(), candidate_id))
        if response:
            wa_send_text(conv['candidate_phone'], response)
            _log_outbound(conn, conv_id, response, 'update_notice')
        return {'action': 'update_notice', 'auto': True, 'notice': notice}

    if action == 'update_ctc':
        ctc = classification.get('extracted_data', {}).get('ctc')
        if ctc:
            conn.execute('UPDATE candidates SET ctc_current=?, updated_at=? WHERE id=?',
                         (ctc, ts(), candidate_id))
        if response:
            wa_send_text(conv['candidate_phone'], response)
            _log_outbound(conn, conv_id, response, 'update_ctc')
        return {'action': 'update_ctc', 'auto': True, 'ctc': ctc}

    # ── Schedule call (create a reminder/task) ──
    if action == 'schedule_call':
        call_time = classification.get('extracted_data', {}).get('call_time', '')
        if response:
            wa_send_text(conv['candidate_phone'], response)
            _log_outbound(conn, conv_id, response, 'schedule_call')
        # Create a reminder for the recruiter
        try:
            owner = conn.execute('SELECT owner_id FROM candidates WHERE id=?',
                                 (candidate_id,)).fetchone()
            due = datetime.datetime.now() + datetime.timedelta(hours=1)  # default 1hr from now
            conn.execute(
                'INSERT INTO reminders (candidate_id,mandate_id,owner_id,candidate_name,'
                'mandate_label,note,due_at,done,created_at) VALUES (?,?,?,?,?,?,?,0,?)',
                (candidate_id, mandate_id, owner['owner_id'] if owner else 0,
                 conv['candidate_name'], conv['role'] or '',
                 f'Call scheduled via WhatsApp: {call_time or "ASAP"}',
                 due.isoformat(), ts()))
        except Exception:
            pass
        return {'action': 'schedule_call', 'auto': True}

    # ── Known response exists but just text reply ──
    if response:
        wa_send_text(conv['candidate_phone'], response)
        _log_outbound(conn, conv_id, response, intent)
        return {'action': intent, 'auto': True}

    # ── ESCALATE: AI doesn't know → ask recruiter ──
    ai_suggestion = generate_response(
        candidate_msg, role=conv['role'] or '', client=conv['client'] or '',
        location=conv['location'] or '', jd_summary=(conv['jd'] or '')[:300],
        deepseek_key=deepseek_key)
    _escalate(conn, conv, msg_id, candidate_msg, '', ai_suggestion)
    return {'action': 'escalated', 'auto': False, 'ai_suggestion': ai_suggestion}


def _log_outbound(conn, conv_id, content, action):
    conn.execute(
        'INSERT INTO wa_messages (conversation_id,direction,sender,content,message_type,'
        'ai_action_taken,delivered,created_at) VALUES (?,?,?,?,?,?,1,?)',
        (conv_id, 'outbound', 'AI Agent', content, 'text', action, ts()))


def _escalate(conn, conv, msg_id, question, reason='', ai_suggestion=''):
    """Create an escalation for the recruiter to handle."""
    conn.execute(
        'INSERT INTO wa_escalations (company_id,conversation_id,message_id,candidate_question,'
        'ai_suggested_response,status,created_at) VALUES (?,?,?,?,?,?,?)',
        (conv['company_id'], conv['id'], msg_id, question, ai_suggestion, 'pending', ts()))
    conn.execute('UPDATE wa_conversations SET escalated=1, escalation_reason=?, updated_at=? WHERE id=?',
                 (reason or question[:200], ts(), conv['id']))


def _send_jd_email(conn, candidate_id, role, jd_text):
    """Send JD via email too (agentic: both channels)."""
    try:
        c = conn.execute('SELECT email FROM candidates WHERE id=?', (candidate_id,)).fetchone()
        if c and c['email']:
            from modules.shared import _core
            core = _core()
            if hasattr(core, '_smtp_send'):
                subject = f'Job Description — {role}'
                core._smtp_send(c['email'], subject, jd_text,
                                f'<div style="font-family:sans-serif;font-size:14px;line-height:1.6">'
                                f'{jd_text.replace(chr(10), "<br>")}</div>')
    except Exception as e:
        print(f'[wa_agent] JD email failed: {e}')


# ══════════════════════════════════════════════════════════════════════════
#  QUALIFICATION FLOW  (Rules Q.1 - Q.7)
# ══════════════════════════════════════════════════════════════════════════
# State machine per conversation. wa_conversations.qualification_state stores
# a JSON dict tracking which step is pending and answers collected so far:
#   { "step": "awaiting_permission" | "company" | "current_ctc" | ...
#                                    | "confirmation" | "done",
#     "collected": { "company": "...", "current_ctc": 18, ... },
#     "retries": { "company": 0, ... },
#     "skipped": ["notice_period", ...]
#   }
# Steps run in this order; already-known fields are skipped (Rule Q.1).

QUAL_STEPS = [
    ('company', 'company',
     'Great! To help match your profile better, could you please share your current company name?'),
    ('current_ctc', 'current_ctc',
     'Could you please share your current CTC (in LPA)?'),
    ('expected_ctc', 'expected_ctc',
     'What is your expected CTC for your next opportunity (in LPA)?'),
    ('notice_period', 'notice_period',
     'What is your current notice period (in days)?'),
    ('current_location', 'location',
     'What is your current location?'),
    ('resume_status', None,
     'One last thing \u2014 have you recently updated your resume? (Yes/No)'),
]

FIELD_LABELS = {
    'company': 'Current Company',
    'current_ctc': 'Current CTC',
    'expected_ctc': 'Expected CTC',
    'notice_period': 'Notice Period',
    'current_location': 'Current Location',
    'resume_status': 'Resume Status',
}


def _within_business_hours():
    """Time-of-day guard (Rule N.5): 8am - 10pm local time."""
    hr = datetime.datetime.now().hour
    return 8 <= hr < 22


def _parse_qual_state(conv):
    """Read qualification_state JSON from conversation row (safe defaults)."""
    try:
        raw = conv['qualification_state'] if 'qualification_state' in conv.keys() else ''
    except Exception:
        raw = ''
    if not raw:
        return {'step': '', 'collected': {}, 'retries': {}, 'skipped': []}
    try:
        return json.loads(raw)
    except Exception:
        return {'step': '', 'collected': {}, 'retries': {}, 'skipped': []}


def _save_qual_state(conn, conv_id, state):
    conn.execute('UPDATE wa_conversations SET qualification_state=?, updated_at=? WHERE id=?',
                 (json.dumps(state), ts(), conv_id))


def _candidate_field_value(conn, candidate_id, step_key):
    """Read the corresponding ATS field to check if it's already filled (Rule Q.1)."""
    field_map = {
        'company': 'company',
        'current_ctc': 'ctc_current',
        'expected_ctc': 'ctc_expected',
        'notice_period': 'notice_period',
        'current_location': 'location',
    }
    col = field_map.get(step_key)
    if not col:
        return None
    row = conn.execute(f'SELECT {col} AS v FROM candidates WHERE id=?', (candidate_id,)).fetchone()
    if not row:
        return None
    v = row['v']
    # Numeric fields: 0 is "empty"
    if step_key in ('current_ctc', 'expected_ctc', 'notice_period'):
        try:
            return float(v) if v and float(v) > 0 else None
        except Exception:
            return None
    return v if v else None


def _next_pending_step(conn, conv, state):
    """Find the next qualification step whose ATS field is still empty."""
    for step_key, _, _ in QUAL_STEPS:
        if step_key in state.get('collected', {}):
            continue  # already collected in this flow
        if step_key in state.get('skipped', []):
            continue
        # Resume status is never in ATS as a plain column — always ask
        if step_key == 'resume_status':
            return step_key
        existing = _candidate_field_value(conn, conv['candidate_id'], step_key)
        if existing not in (None, '', 0):
            # Field already known — record it as collected without asking
            state['collected'][step_key] = existing
            continue
        return step_key
    return None


def _q_prompt(step_key):
    for k, _, prompt in QUAL_STEPS:
        if k == step_key:
            return prompt
    return ''


def _parse_answer(step_key, msg):
    """Extract a usable value from a candidate's free-text answer.
    Returns (value, is_non_answer_bool)."""
    text = (msg or '').strip()
    if not text:
        return None, True

    low = text.lower()
    # Detect non-answers (Tweak 1: non-answer handling)
    non_answers = ['depends', 'tell you later', "i'll tell", 'discuss on call',
                   'on call', 'not now', 'not sure', 'confidential', 'private',
                   "won't share", 'wont share', 'later', "can't say", 'cant say']
    if any(na in low for na in non_answers) and len(text) < 60:
        return None, True

    if step_key == 'company':
        # Reject obvious non-company answers
        if len(text) < 2 or len(text) > 80:
            return None, True
        return text, False

    if step_key in ('current_ctc', 'expected_ctc'):
        # Extract number (LPA)
        m = re.search(r'(\d+(?:\.\d+)?)', text.replace(',', ''))
        if not m:
            return None, True
        val = float(m.group(1))
        # Sanity: LPA usually 1-200
        if val < 0.5 or val > 500:
            return None, True
        return val, False

    if step_key == 'notice_period':
        m = re.search(r'(\d+)', text)
        if not m:
            # "immediate", "immediately", "serving" etc.
            if any(w in low for w in ['immediate', 'serving', 'notice served', '0 day', 'zero']):
                return 0, False
            if 'month' in low:
                return None, True
            return None, True
        val = int(m.group(1))
        if 'month' in low:
            val *= 30
        if val < 0 or val > 365:
            return None, True
        return val, False

    if step_key == 'current_location':
        if len(text) < 2 or len(text) > 80:
            return None, True
        return text, False

    if step_key == 'resume_status':
        if any(w in low for w in ['yes', 'yep', 'yeah', 'haan', 'ha', 'updated', 'recent']):
            return 'updated', False
        if any(w in low for w in ['no', 'nope', 'nahi', 'nahin', 'not yet', 'old']):
            return 'not_updated', False
        return None, True

    return text, False


def _save_field_to_ats(conn, candidate_id, step_key, value):
    """Write the collected value into the corresponding ATS column + journey."""
    field_map = {
        'company': 'company',
        'current_ctc': 'ctc_current',
        'expected_ctc': 'ctc_expected',
        'notice_period': 'notice_period',
        'current_location': 'location',
    }
    col = field_map.get(step_key)
    if col:
        conn.execute(f'UPDATE candidates SET {col}=?, updated_at=? WHERE id=?',
                     (value, ts(), candidate_id))
    log_activity(f'wa.qual.{step_key}',
                 f'{FIELD_LABELS.get(step_key, step_key)} updated via WhatsApp AI: {value}',
                 entity_type='candidate', entity_id=candidate_id,
                 meta={'step': step_key, 'value': value})


def start_qualification_flow(conn, conv_id):
    """Recruiter clicks 'Start Qualification' → send permission ask (Tweak Q.2).
    Only proceeds after candidate says yes."""
    conv = conn.execute(
        'SELECT c.*, m.role FROM wa_conversations c '
        'LEFT JOIN mandates m ON m.id=c.mandate_id WHERE c.id=?',
        (conv_id,)).fetchone()
    if not conv:
        return False, 'Conversation not found'
    if conv['status'] != 'active':
        return False, 'Conversation is not active'

    state = _parse_qual_state(conv)
    if state.get('step') and state['step'] != 'done':
        return False, f'Qualification already in progress (step: {state["step"]})'

    # Time-of-day guard (Rule N.5)
    if not _within_business_hours():
        return False, 'Qualification is queued for 9am (currently outside business hours).'

    state = {'step': 'awaiting_permission', 'collected': {}, 'retries': {}, 'skipped': []}
    _save_qual_state(conn, conv_id, state)

    msg = (f"I have a few quick questions to help match your profile better \u2014 "
           f"takes about 2 minutes. Shall I proceed?")
    ok, _ = wa_send_text(conv['candidate_phone'], msg)
    _log_outbound(conn, conv_id, msg, 'qual.permission_ask')
    conn.commit()
    log_activity('wa.qual.started', 'Qualification flow initiated by recruiter',
                 entity_type='candidate', entity_id=conv['candidate_id'])
    return ok, 'Permission message sent'


def _process_qualification_reply(conn, conv, msg_id, message_text):
    """Called from process_incoming_message when a qualification is in progress.
    Returns True if the message was handled here (skip normal AI routing)."""
    state = _parse_qual_state(conv)
    step = state.get('step', '')
    if not step:
        return False  # no active qualification

    conv_id = conv['id']
    candidate_id = conv['candidate_id']
    text = (message_text or '').strip()
    low = text.lower()

    # ── Awaiting permission (Tweak Q.2) ─────────────────────────────────────
    if step == 'awaiting_permission':
        # Accept explicit YES; anything else = candidate prefers not now
        if any(w in low for w in ['yes', 'yep', 'yeah', 'sure', 'ok', 'okay',
                                   'proceed', 'go ahead', 'haan', 'ha', 'ji']):
            # Find first missing step and ask
            next_step = _next_pending_step(conn, conv, state)
            if not next_step:
                # Nothing to ask — all fields already known
                _finalize_qualification(conn, conv, state)
                return True
            state['step'] = next_step
            _save_qual_state(conn, conv_id, state)
            prompt = _q_prompt(next_step)
            wa_send_text(conv['candidate_phone'], prompt)
            _log_outbound(conn, conv_id, prompt, f'qual.ask.{next_step}')
            conn.commit()
            return True
        else:
            # Candidate declined or wants a call — escalate
            state['step'] = ''
            _save_qual_state(conn, conv_id, state)
            _escalate(conn, conv, msg_id, message_text,
                      'Candidate declined qualification questions \u2014 may prefer a call')
            conn.commit()
            return True

    # ── Confirmation summary (Tweak Q.3) ────────────────────────────────────
    if step == 'confirmation':
        if any(w in low for w in ['yes', 'yep', 'correct', 'right', 'sahi',
                                   'confirm', 'ok', 'okay', 'haan', 'ji']):
            _finalize_qualification(conn, conv, state)
            return True
        if any(w in low for w in ['no', 'wrong', 'incorrect', 'change', 'edit',
                                   'nahi', 'galat']):
            # Escalate so recruiter can help correct
            state['step'] = ''
            _save_qual_state(conn, conv_id, state)
            _escalate(conn, conv, msg_id, message_text,
                      'Candidate says qualification summary needs correction')
            conn.commit()
            return True
        # Unclear — escalate rather than loop
        state['step'] = ''
        _save_qual_state(conn, conv_id, state)
        _escalate(conn, conv, msg_id, message_text,
                  'Unclear reply to confirmation summary')
        conn.commit()
        return True

    # ── Resume update pending confirmation ─────────────────────────────────
    if step == 'awaiting_resume_done':
        if any(w in low for w in ['done', 'updated', 'ho gaya', 'hogaya',
                                   'complete', 'finished']):
            state['collected']['resume_status'] = 'updated_via_link'
            log_activity('wa.qual.resume_done',
                         'Candidate confirmed resume updated via self-update link',
                         entity_type='candidate', entity_id=candidate_id)
            _send_confirmation_summary(conn, conv, state)
            return True
        # Not "done" yet — polite nudge, no state change
        wa_send_text(conv['candidate_phone'],
                     "No worries \u2014 whenever you've updated, just reply 'Done' here.")
        _log_outbound(conn, conv['id'], 'Nudge for resume done reply', 'qual.resume_nudge')
        conn.commit()
        return True

    # ── Active data-collection step ─────────────────────────────────────────
    if step in [k for k, _, _ in QUAL_STEPS]:
        value, is_non_answer = _parse_answer(step, message_text)

        if is_non_answer:
            # Tweak 1: non-answer handling — retry once, then skip or escalate
            retries = state.get('retries', {})
            n = retries.get(step, 0) + 1
            retries[step] = n
            state['retries'] = retries

            if n == 1:
                # Retry once with a gentler prompt
                nudge = _retry_prompt(step)
                wa_send_text(conv['candidate_phone'], nudge)
                _log_outbound(conn, conv_id, nudge, f'qual.retry.{step}')
                _save_qual_state(conn, conv_id, state)
                conn.commit()
                return True
            else:
                # Skip this field, move on
                state['skipped'] = state.get('skipped', []) + [step]
                # Send explicit acknowledgment so candidate knows we moved on
                skip_msg = "No worries, let's move on."
                wa_send_text(conv['candidate_phone'], skip_msg)
                _log_outbound(conn, conv_id, skip_msg, f'qual.skip.{step}')
                log_activity('wa.qual.skipped',
                             f'Skipped {FIELD_LABELS.get(step, step)} \u2014 candidate did not give a clear answer',
                             entity_type='candidate', entity_id=candidate_id,
                             meta={'step': step, 'last_reply': message_text[:200]})
                _advance_or_finalize(conn, conv, state, msg_id)
                return True

        # Special: resume_status branching (Step 6)
        if step == 'resume_status':
            state['collected']['resume_status'] = value
            if value == 'updated':
                # Just log, done
                log_activity('wa.qual.resume_naukri',
                             'Candidate confirmed updated resume on Naukri',
                             entity_type='candidate', entity_id=candidate_id)
                _send_confirmation_summary(conn, conv, state)
                return True
            else:
                # Send self-update link + wait for "Done"
                _send_resume_update_link(conn, conv)
                state['step'] = 'awaiting_resume_done'
                _save_qual_state(conn, conv_id, state)
                conn.commit()
                return True

        # Normal field collection
        state['collected'][step] = value
        _save_field_to_ats(conn, candidate_id, step, value)
        _advance_or_finalize(conn, conv, state, msg_id)
        return True

    return False  # step string not recognized — fall through


def _retry_prompt(step_key):
    """Slightly softer re-ask if the first attempt returned a non-answer."""
    softer = {
        'company': "No problem \u2014 just the name of your current employer would be great.",
        'current_ctc': "Understood \u2014 an approximate current CTC (in LPA) would help us. Even a rough number works.",
        'expected_ctc': "A rough expected CTC (in LPA) is fine \u2014 just to see if it fits the role.",
        'notice_period': "Could you share a rough number of days for your notice period? (or say 'immediate' if you're free to join now)",
        'current_location': "Just the city where you're currently based would help.",
    }
    return softer.get(step_key, _q_prompt(step_key))


def _advance_or_finalize(conn, conv, state, msg_id):
    """After a field is collected/skipped, ask the next one or finalize."""
    next_step = _next_pending_step(conn, conv, state)
    if next_step:
        state['step'] = next_step
        _save_qual_state(conn, conv['id'], state)
        prompt = _q_prompt(next_step)
        wa_send_text(conv['candidate_phone'], prompt)
        _log_outbound(conn, conv['id'], prompt, f'qual.ask.{next_step}')
        conn.commit()
    else:
        _send_confirmation_summary(conn, conv, state)


def _send_confirmation_summary(conn, conv, state):
    """Tweak Q.3: read back everything before finalizing."""
    collected = state.get('collected', {})
    lines = []
    for key, _, _ in QUAL_STEPS:
        if key not in collected:
            continue
        val = collected[key]
        label = FIELD_LABELS.get(key, key)
        if key == 'resume_status':
            display = 'Updated' if val in ('updated', 'updated_via_link') else 'Not updated'
        elif key in ('current_ctc', 'expected_ctc'):
            display = f'{val} LPA'
        elif key == 'notice_period':
            display = 'Immediate' if val == 0 else f'{val} days'
        else:
            display = str(val)
        lines.append(f'\u2022 {label}: {display}')

    if not lines:
        # Nothing was collected — just finalize silently
        _finalize_qualification(conn, conv, state)
        return

    summary = ('Just to confirm the details you shared:\n\n'
               + '\n'.join(lines)
               + '\n\nIs this all correct? (Yes/No)')
    wa_send_text(conv['candidate_phone'], summary)
    _log_outbound(conn, conv['id'], summary, 'qual.confirmation')
    state['step'] = 'confirmation'
    _save_qual_state(conn, conv['id'], state)
    conn.commit()


def _finalize_qualification(conn, conv, state):
    """All info collected, candidate confirmed \u2014 wrap up + notify recruiter."""
    conv_id = conv['id']
    candidate_id = conv['candidate_id']

    # Final thank-you message
    thanks = ("Thank you! I have all the details I need. Our recruiter will "
              "reach out shortly to discuss the next steps.")
    wa_send_text(conv['candidate_phone'], thanks)
    _log_outbound(conn, conv_id, thanks, 'qual.finalize')

    # Mark state done
    state['step'] = 'done'
    _save_qual_state(conn, conv_id, state)

    # Move candidate to "Interested" if not already there
    try:
        row = conn.execute('SELECT stage FROM candidates WHERE id=?', (candidate_id,)).fetchone()
        if row and row['stage'] not in ('Interested', 'Shared with Client',
                                         'Interview Inprocess', 'Placed'):
            conn.execute("UPDATE candidates SET stage='Interested', updated_at=? WHERE id=?",
                         (ts(), candidate_id))
    except Exception:
        pass

    # Create a task for the recruiter to follow up
    try:
        owner = conn.execute('SELECT owner_id FROM candidates WHERE id=?',
                             (candidate_id,)).fetchone()
        due = datetime.datetime.now() + datetime.timedelta(hours=2)
        conn.execute(
            'INSERT INTO reminders (candidate_id,mandate_id,owner_id,candidate_name,'
            'mandate_label,note,due_at,done,created_at) VALUES (?,?,?,?,?,?,?,0,?)',
            (candidate_id, conv['mandate_id'], owner['owner_id'] if owner else 0,
             conv['candidate_name'], '',
             'WhatsApp qualification complete \u2014 review and call candidate',
             due.isoformat(), ts()))
    except Exception:
        pass

    collected = state.get('collected', {})
    skipped = state.get('skipped', [])
    log_activity('wa.qual.completed',
                 f'WhatsApp qualification completed. Collected: {len(collected)} fields, '
                 f'Skipped: {len(skipped)}',
                 entity_type='candidate', entity_id=candidate_id,
                 meta={'collected': collected, 'skipped': skipped})
    conn.commit()


def _send_resume_update_link(conn, conv):
    """Step 6 branch: candidate said resume not updated \u2014 send our self-update link
    (candidate lands on our page, we track submission)."""
    candidate_id = conv['candidate_id']
    # Reuse the existing request-update endpoint (generates token + emails link)
    try:
        from modules.shared import _core
        core = _core()
        # Ensure token
        c = conn.execute(
            'SELECT update_token FROM candidates WHERE id=?', (candidate_id,)).fetchone()
        token = c['update_token'] if c else ''
        if not token:
            import secrets
            token = secrets.token_urlsafe(24)
            conn.execute('UPDATE candidates SET update_token=?, update_requested_at=? WHERE id=?',
                         (token, ts(), candidate_id))
            conn.commit()
        # Build the public link
        base = os.environ.get('APP_BASE_URL', '').strip() or 'https://hirelabscreener.onrender.com'
        link = f'{base}/update-profile?token={token}'
        msg = (f"No problem. Please update your profile using this quick link \u2014 "
               f"it takes 2 minutes:\n\n{link}\n\nOnce you're done, just reply 'Done' here.")
        wa_send_text(conv['candidate_phone'], msg)
        _log_outbound(conn, conv['id'], msg, 'qual.resume_link')
        # Also email if we have an email
        cand = conn.execute('SELECT name, email FROM candidates WHERE id=?',
                            (candidate_id,)).fetchone()
        if cand and cand['email'] and hasattr(core, '_smtp_send'):
            core._smtp_send(cand['email'],
                            'Please update your profile',
                            f'Hi {cand["name"] or "there"},\n\nPlease update your profile '
                            f'using this link:\n\n{link}\n\nThank you.',
                            None)
        log_activity('wa.qual.resume_link_sent',
                     'Resume self-update link sent via WhatsApp + Email',
                     entity_type='candidate', entity_id=candidate_id)
    except Exception as e:
        print(f'[wa_agent] resume link send failed: {e}')


import os  # for APP_BASE_URL


# ══════════════════════════════════════════════════════════════════════════
#  API ROUTES
# ══════════════════════════════════════════════════════════════════════════

# ── Webhook: Meta WhatsApp Cloud API sends incoming messages here ────────
@bp.route('/webhook', methods=['GET'])
def wa_webhook_verify():
    """Meta's webhook verification challenge (one-time setup)."""
    cfg = _wa_config()
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    if mode == 'subscribe' and token == cfg['verify_token']:
        return challenge, 200
    return 'Forbidden', 403


@bp.route('/webhook', methods=['POST'])
def wa_webhook_receive():
    """Receive incoming WhatsApp messages from Meta's webhook."""
    data = request.json or {}
    try:
        for entry in data.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value', {})
                for msg in value.get('messages', []):
                    if msg.get('type') == 'text':
                        phone = msg.get('from', '')
                        text = msg.get('text', {}).get('body', '')
                        wa_id = msg.get('id', '')
                        if phone and text:
                            conn = get_db()
                            process_incoming_message(conn, phone, text, wa_id)
                            conn.close()
    except Exception as e:
        print(f'[wa_agent] webhook error: {e}')
    return 'OK', 200


# ── Start a conversation (called when candidate is added or manually) ────
@bp.route('/start', methods=['POST'])
@login_required
def api_start_conversation():
    d = request.json or {}
    candidate_id = d.get('candidate_id')
    mandate_id = d.get('mandate_id')
    if not candidate_id or not mandate_id:
        return jsonify({'error': 'candidate_id and mandate_id required'}), 400
    conn = get_db()
    ok, result = start_conversation(conn, candidate_id, mandate_id)
    conn.close()
    if ok:
        return jsonify({'ok': True, 'conversation_id': result})
    return jsonify({'error': result}), 400


# ── Retry an intro that failed (e.g. recruiter fixed the phone number) ───
@bp.route('/conversations/<int:conv_id>/retry', methods=['POST'])
@login_required
def api_retry_conversation(conv_id):
    conn = get_db()
    conv = conn.execute('SELECT * FROM wa_conversations WHERE id=?', (conv_id,)).fetchone()
    if not conv:
        conn.close(); return jsonify({'error': 'Conversation not found'}), 404
    if conv['company_id'] != effective_company_id():
        conn.close(); return jsonify({'error': 'Not permitted'}), 403
    # Delete the failed record and start fresh
    conn.execute('DELETE FROM wa_conversations WHERE id=?', (conv_id,))
    conn.commit()
    ok, result = start_conversation(conn, conv['candidate_id'], conv['mandate_id'])
    conn.close()
    if ok:
        return jsonify({'ok': True, 'conversation_id': result})
    return jsonify({'error': result}), 400


# ── Start qualification flow (Rule Q.2 permission ask) ───────────────────
@bp.route('/conversations/<int:conv_id>/start-qualification', methods=['POST'])
@login_required
def api_start_qualification(conv_id):
    conn = get_db()
    conv = conn.execute('SELECT company_id FROM wa_conversations WHERE id=?',
                        (conv_id,)).fetchone()
    if not conv:
        conn.close(); return jsonify({'error': 'Conversation not found'}), 404
    if conv['company_id'] != effective_company_id():
        conn.close(); return jsonify({'error': 'Not permitted'}), 403
    ok, msg = start_qualification_flow(conn, conv_id)
    conn.close()
    if ok:
        return jsonify({'ok': True, 'message': msg})
    return jsonify({'error': msg}), 400



# ── List conversations (for the recruiter dashboard) ─────────────────────
@bp.route('/conversations', methods=['GET'])
@login_required
def list_conversations():
    company_id = effective_company_id()
    status = request.args.get('status', 'active')
    conn = get_db()
    rows = conn.execute(
        'SELECT c.*, m.role, m.client FROM wa_conversations c '
        'LEFT JOIN mandates m ON m.id=c.mandate_id '
        'WHERE c.company_id=? AND c.status=? ORDER BY c.last_message_at DESC',
        (company_id, status)).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        # Get last message
        last = conn.execute(
            'SELECT content, direction, created_at FROM wa_messages '
            'WHERE conversation_id=? ORDER BY id DESC LIMIT 1', (r['id'],)).fetchone()
        d['last_message'] = dict(last) if last else None
        # Pending escalation?
        esc = conn.execute(
            "SELECT COUNT(*) n FROM wa_escalations WHERE conversation_id=? AND status='pending'",
            (r['id'],)).fetchone()
        d['pending_escalations'] = esc['n']
        out.append(d)

    conn.close()
    return jsonify({'ok': True, 'conversations': out})


# ── Get conversation messages ────────────────────────────────────────────
@bp.route('/conversations/<int:conv_id>/messages', methods=['GET'])
@login_required
def get_messages(conv_id):
    conn = get_db()
    conv = conn.execute('SELECT company_id FROM wa_conversations WHERE id=?', (conv_id,)).fetchone()
    if not conv or conv['company_id'] != effective_company_id():
        conn.close(); return jsonify({'error': 'Not found'}), 404
    messages = conn.execute(
        'SELECT * FROM wa_messages WHERE conversation_id=? ORDER BY id ASC',
        (conv_id,)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'messages': [dict(m) for m in messages]})


# ── Pending escalations (AI asking recruiter for help) ───────────────────
@bp.route('/escalations', methods=['GET'])
@login_required
def list_escalations():
    company_id = effective_company_id()
    conn = get_db()
    rows = conn.execute(
        "SELECT e.*, c.candidate_name, c.candidate_phone, m.role, m.client "
        "FROM wa_escalations e "
        "JOIN wa_conversations c ON c.id=e.conversation_id "
        "LEFT JOIN mandates m ON m.id=c.mandate_id "
        "WHERE e.company_id=? AND e.status='pending' ORDER BY e.id DESC",
        (company_id,)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'escalations': [dict(r) for r in rows]})


# ── Resolve escalation (recruiter answers + optionally teaches AI) ───────
@bp.route('/escalations/<int:esc_id>/resolve', methods=['POST'])
@login_required
def resolve_escalation(esc_id):
    d = request.json or {}
    response_text = (d.get('response') or '').strip()
    learn = d.get('learn', False)  # Should AI learn this pattern for the future?
    intent_label = (d.get('intent_label') or '').strip()  # What intent was this?

    if not response_text:
        return jsonify({'error': 'Response text required'}), 400

    conn = get_db()
    esc = conn.execute('SELECT * FROM wa_escalations WHERE id=?', (esc_id,)).fetchone()
    if not esc:
        conn.close(); return jsonify({'error': 'Escalation not found'}), 404

    # Send the response to the candidate
    conv = conn.execute('SELECT * FROM wa_conversations WHERE id=?',
                        (esc['conversation_id'],)).fetchone()
    if conv:
        wa_send_text(conv['candidate_phone'], response_text)
        _log_outbound(conn, conv['id'], response_text, 'recruiter_reply')
        conn.execute('UPDATE wa_conversations SET escalated=0, updated_at=? WHERE id=?',
                     (ts(), conv['id']))

    # Mark escalation resolved
    conn.execute(
        'UPDATE wa_escalations SET status=?, recruiter_response=?, learn_as_pattern=?, '
        'resolved_by=?, resolved_at=? WHERE id=?',
        ('resolved', response_text, 1 if learn else 0, real_user_id(), ts(), esc_id))

    # LEARNING LOOP: if recruiter says "learn this", store for future use
    if learn and intent_label:
        company_id = effective_company_id()
        conn.execute(
            'INSERT INTO wa_learned_responses (company_id,intent_pattern,sample_question,'
            'response_template,action,taught_by,is_active,created_at,updated_at) '
            'VALUES (?,?,?,?,?,?,1,?,?)',
            (company_id, intent_label, esc['candidate_question'], response_text,
             'auto_reply', real_user_id(), ts(), ts()))
        log_activity('wa.ai_learned',
                     f'AI learned new response for "{intent_label}"',
                     meta={'sample': esc['candidate_question'][:100], 'intent': intent_label})

    conn.commit(); conn.close()
    return jsonify({'ok': True})


# ── Manual send (recruiter types a message) ──────────────────────────────
@bp.route('/conversations/<int:conv_id>/send', methods=['POST'])
@login_required
def manual_send(conv_id):
    d = request.json or {}
    text = (d.get('message') or '').strip()
    if not text:
        return jsonify({'error': 'Message required'}), 400
    conn = get_db()
    conv = conn.execute('SELECT * FROM wa_conversations WHERE id=?', (conv_id,)).fetchone()
    if not conv or conv['company_id'] != effective_company_id():
        conn.close(); return jsonify({'error': 'Not found'}), 404
    ok, wa_id = wa_send_text(conv['candidate_phone'], text)
    u = current_user()
    conn.execute(
        'INSERT INTO wa_messages (conversation_id,direction,sender,content,message_type,'
        'wa_message_id,ai_action_taken,delivered,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
        (conv_id, 'outbound', u['username'] if u else 'Recruiter', text, 'text',
         wa_id if ok else '', 'manual', 1 if ok else 0, ts()))
    conn.execute('UPDATE wa_conversations SET last_message_at=?, updated_at=? WHERE id=?',
                 (ts(), ts(), conv_id))
    conn.commit(); conn.close()
    if ok:
        return jsonify({'ok': True})
    return jsonify({'error': 'Message saved but WhatsApp send failed'}), 400


# ── Learned responses management (view/edit the AI's knowledge base) ─────
@bp.route('/learned', methods=['GET'])
@login_required
def list_learned():
    company_id = effective_company_id()
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM wa_learned_responses WHERE (company_id=? OR company_id=0) AND is_active=1 '
        'ORDER BY use_count DESC', (company_id,)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'responses': [dict(r) for r in rows]})


@bp.route('/learned', methods=['POST'])
@login_required
def add_learned():
    d = request.json or {}
    intent = (d.get('intent_pattern') or '').strip()
    sample = (d.get('sample_question') or '').strip()
    response = (d.get('response_template') or '').strip()
    action = (d.get('action') or 'auto_reply').strip()
    if not intent or not response:
        return jsonify({'error': 'intent_pattern and response_template required'}), 400
    conn = get_db()
    conn.execute(
        'INSERT INTO wa_learned_responses (company_id,intent_pattern,sample_question,'
        'response_template,action,taught_by,is_active,created_at,updated_at) '
        'VALUES (?,?,?,?,?,?,1,?,?)',
        (effective_company_id(), intent, sample, response, action, real_user_id(), ts(), ts()))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@bp.route('/learned/<int:lid>', methods=['DELETE'])
@login_required
def delete_learned(lid):
    conn = get_db()
    conn.execute('UPDATE wa_learned_responses SET is_active=0, updated_at=? WHERE id=?', (ts(), lid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})
