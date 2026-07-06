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
    if not c or not c['phone']:
        return False, 'Candidate has no phone number'

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
        conn.execute("UPDATE wa_conversations SET status='closed', updated_at=? WHERE id=?",
                     (ts(), conv_id))
        log_activity('wa.not_interested', f'{conv["candidate_name"]} marked not interested via WhatsApp',
                     entity_type='candidate', entity_id=candidate_id)
        return {'action': 'mark_not_interested', 'auto': True}

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
