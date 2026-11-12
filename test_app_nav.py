"""
The menu on the separate pages (modules/app_nav.py) is the SAME as the main
app's sidebar: it is read from index.html, so the two can never drift apart.

Run:   pytest test_app_nav.py -q
"""
import os
import re
import sys
import json
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.abspath(__file__))


def index_html():
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        return f.read()


def test_menu_is_the_main_sidebar_in_order():
    from modules.app_nav import parse_sidebar, ALWAYS_HIDDEN
    page = index_html()
    side = page[page.index('<div id="sidebar">'):page.index('<div id="main"')]
    in_app = [i for i in re.findall(r'<a\b[^>]*\bclass="sb-gear[^"]*"[^>]*\bid="(sb-[a-z0-9-]+)"', side)
              if i not in ALWAYS_HIDDEN]
    menu = parse_sidebar(page)
    assert [x['id'] for x in menu['items']] == in_app
    by = {x['id']: x for x in menu['items']}
    # names, links and icons come straight from the sidebar
    assert by['sb-home']['title'] == 'Home' and by['sb-home']['href'] == '/?go=home'
    assert by['sb-email']['title'] == 'Email Box' and by['sb-email']['href'] == '/?go=email'
    assert by['sb-teamactivity']['href'] == '/team-activity' and by['sb-teamactivity']['rs_hide']
    assert by['sb-admin']['title'] == 'Admin Panel' and by['sb-admin']['bottom']
    assert by['sb-gear']['title'] == 'Settings' and by['sb-gear']['bottom']
    assert all(x['svg'] and x['title'] for x in menu['items'])
    assert 'BUILD' in menu['build']


def test_items_the_app_force_hides_are_never_in_the_menu():
    from modules.app_nav import ALWAYS_HIDDEN, parse_sidebar
    page = index_html()
    forced = set(re.findall(r'#sidebar #(sb-[a-z0-9-]+)', ''.join(
        re.findall(r'(#sidebar #sb-[^{]*)\{display:none !important\}', page))))
    assert forced <= ALWAYS_HIDDEN
    assert not ({x['id'] for x in parse_sidebar(page)['items']} & ALWAYS_HIDDEN)


def test_every_menu_link_opens_something():
    """/?go=<x> clicks #sb-<x> in the app; a page link must be a real route."""
    from modules.app_nav import parse_sidebar
    page = index_html()
    ids = set(re.findall(r'\bid="(sb-[a-z0-9-]+)"', page))
    for it in parse_sidebar(page)['items']:
        if it['href'].startswith('/?go='):
            assert 'sb-' + it['href'][5:] in ids, it


@pytest.fixture(scope='module')
def app():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='appnav_'))
    import server
    server.app.config['TESTING'] = True
    return server.app


def test_script_serves_the_menu(app):
    r = app.test_client().get('/app-nav.js')
    assert r.status_code == 200 and r.mimetype == 'application/javascript'
    js = r.get_data(as_text=True)
    m = re.search(r'var MENU = (\{.*?\});\n', js, re.S)
    data = json.loads(m.group(1).replace('<\\/', '</'))
    assert [x['title'] for x in data['items']][:3] == ['Home', 'Jobs', 'Tasks']
    assert '__MENU__' not in js and '</script' not in js.lower()
