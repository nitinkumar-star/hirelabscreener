"""
Nov 2026: replies looked messy. The page used to put the whole earlier mail into
the reply as "> " text lines (above the signature). Now the reply box starts
empty and the server adds the earlier mail as a proper Gmail-style quote, below
the signature, with the quoted mail's own <style>/<head> removed.

Run:   pytest test_reply_quote.py -q      (no real SMTP is touched)
"""
import os
import sys
import tempfile
import email

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CO, ADMIN = 961, 9601


class FakeSMTP(object):
    sent = []

    def __init__(self, host, port, timeout=None):
        pass

    def starttls(self):
        pass

    def login(self, user, pw):
        pass

    def sendmail(self, frm, to, msg):
        FakeSMTP.sent.append(msg)
        return {}

    def send_message(self, msg, *a, **k):
        FakeSMTP.sent.append(msg.as_string())
        return {}

    def quit(self):
        pass

    def close(self):
        pass


def _parts(raw):
    m = email.message_from_string(raw)
    out = {}
    for part in m.walk():
        if part.get_content_maintype() == 'text':
            out[part.get_content_subtype()] = part.get_payload(decode=True).decode('utf-8', 'replace')
    return out


@pytest.fixture(scope='module')
def env():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='replyq_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    import smtplib
    orig = smtplib.SMTP
    smtplib.SMTP = FakeSMTP
    c = server.get_db(); now = server.ts()
    c.execute("INSERT OR REPLACE INTO companies (id, name, status, created_at) VALUES (?,?,?,?)", (CO, 'Reply Co', 'active', now))
    c.execute("INSERT OR REPLACE INTO users (id, username, password_hash, display_name, role, status, company_id, "
              "is_company_admin, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
              (ADMIN, 'rq_admin', 'x', 'Nitin', 'user', 'approved', CO, 1, now))
    for k, v in (('smtp_email', 'hr@replyco.com'), ('smtp_app_password', 'pw'), ('sig_name', 'Nitin Kumar'),
                 ('sig_company', 'Reply Co')):
        c.execute("INSERT OR REPLACE INTO tenant_settings (company_id,key,value) VALUES (?,?,?)", (CO, k, v))
    c.execute("INSERT INTO emails (owner_id,user_id,msg_id,folder,from_addr,from_name,to_addr,subject,body,body_html,"
              "snippet,date_ts,date_str,is_read,created_at) VALUES (?,0,?,?,?,?,?,?,?,?,?,?,?,1,?)",
              (CO, '<orig-1@client.com>', 'Inbox', 'priya@client.com', 'Priya Shah', 'hr@replyco.com',
               'Offer letter', 'Please review the offer.',
               '<html><head><style>body{font-size:30px;color:red}</style></head>'
               '<body onload="x()"><p>Please review the <b>offer</b>.</p></body></html>',
               'Please review the offer.', 1700000000, 'Mon, 23 Nov 2026 10:00', now))
    c.commit(); c.close()
    yield server
    smtplib.SMTP = orig


def _send(server, body, html):
    cl = server.app.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = ADMIN
    FakeSMTP.sent.clear()
    r = cl.post('/api/emailbox/send', json={'to': 'priya@client.com', 'subject': 'Re: Offer letter',
                                             'body': body, 'body_html': html, 'in_reply_to': '<orig-1@client.com>'})
    assert r.status_code == 200, r.get_json()
    assert FakeSMTP.sent
    return _parts(FakeSMTP.sent[-1])


def test_reply_quotes_the_mail_cleanly_below_the_signature(env):
    p = _send(env, 'Thanks Priya, looks good.', '<div>Thanks Priya, <b>looks good</b>.</div>')
    h = p['html']
    assert '<b>looks good</b>' in h
    assert 'class="gmail_quote"' in h and '<blockquote' in h
    assert 'Please review the <b>offer</b>' in h
    assert '<style' not in h and 'font-size:30px' not in h and 'onload' not in h
    assert h.index('Nitin Kumar') < h.index('gmail_quote')           # signature above the quote
    assert '> Please review the offer.' in p['plain']


def test_old_style_quote_from_a_cached_page_is_cut(env):
    old = 'Sure.\n\n--- On 23/11/2026, Priya Shah wrote ---\n> Please review the offer.'
    html = 'Sure.<br><br>--- On 23/11/2026, Priya Shah wrote ---<br>&gt; Please review the offer.'
    p = _send(env, old, html)
    assert '--- On' not in p['html'] and '--- On' not in p['plain']
    assert p['html'].count('Please review the') == 1                  # quoted once, properly
    assert 'gmail_quote' in p['html']


def test_quote_safe_html(env):
    s = env._quote_safe_html('<html><head><title>t</title><style>p{}</style></head>'
                             '<body><p onclick="a()">Hi</p><script>bad()</script></body></html>')
    assert s == '<p>Hi</p>'
