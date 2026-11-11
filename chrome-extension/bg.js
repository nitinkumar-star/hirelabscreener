// HireLab Assistant — background.
// 1) Clicking the toolbar icon opens the side panel.
// 2) Task alerts: every minute, ask HireLab for my timed tasks and meetings
//    due soon, and show a Chrome notification 10 minutes before each one
//    (even when no HireLab tab is open). The ATS "claims" each alert, so the
//    ATS tab and this extension never show the same alert twice.
// There are no content scripts: the Assistant never reads or changes any web page.
const ATS = 'https://hirelabscreener.onrender.com';
const LEAD_MS = 10 * 60 * 1000;
const ALARM = 'hl-task-alerts';
const OFF_KEY = 'hla_alerts_off';

function setup() {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  chrome.alarms.get(ALARM, a => { if (!a) chrome.alarms.create(ALARM, { delayInMinutes: 0.1, periodInMinutes: 1 }); });
}
chrome.runtime.onInstalled.addListener(setup);
chrome.runtime.onStartup.addListener(setup);
setup();

async function json(method, path, body) {
  try {
    const r = await fetch(ATS + path, { method, credentials: 'include',
      headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
    if (!r.ok) return null;
    return await r.json();
  } catch (e) { return null; }
}

function fmt(ms) {
  try { return new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); } catch (e) { return ''; }
}

async function checkAlerts() {
  const off = await chrome.storage.local.get(OFF_KEY);
  if (off && off[OFF_KEY]) return;
  const r = await json('GET', '/api/task-alerts/upcoming?within=12');
  if (!r || !r.ok) return;
  const now = r.now_ms || Date.now();
  for (const t of r.tasks || []) {
    if (!t.due_ms || t.alerted) continue;
    if (t.due_ms - LEAD_MS > now + 30000 || t.due_ms < now - 60000) continue;   // not yet / already started
    const c = await json('POST', '/api/task-alerts/' + t.id + '/claim', { due_at: t.due_at });
    if (!c || !c.ok) continue;
    const mins = Math.max(1, Math.round((t.due_ms - now) / 60000));
    const id = 'hlt-' + t.id + '-' + Date.now();
    const buttons = t.join_url ? [{ title: 'Join meeting' }, { title: 'Open task' }] : [{ title: 'Open task' }];
    await chrome.storage.session.set({ [id]: { join: t.join_url || '', task: t.id } });
    chrome.notifications.create(id, {
      type: 'basic', iconUrl: 'icon128.png', priority: 2, requireInteraction: true,
      title: (t.join_url ? '🎥 ' : '⏰ ') + 'In ' + mins + ' min · ' + fmt(t.due_ms),
      message: t.title || 'Task', contextMessage: 'HireLab', buttons
    });
  }
}

chrome.alarms.onAlarm.addListener(a => { if (a.name === ALARM) checkAlerts(); });

async function openFor(id, which) {
  const v = (await chrome.storage.session.get(id))[id] || {};
  const url = (which === 'join' && v.join) ? v.join : ATS + '/#tasks';
  chrome.tabs.create({ url });
  chrome.notifications.clear(id);
  chrome.storage.session.remove(id);
}
chrome.notifications.onClicked.addListener(id => {
  if (!id.startsWith('hlt-')) return;
  chrome.storage.session.get(id).then(o => openFor(id, (o[id] && o[id].join) ? 'join' : 'task'));
});
chrome.notifications.onButtonClicked.addListener((id, idx) => {
  if (!id.startsWith('hlt-')) return;
  chrome.storage.session.get(id).then(o => {
    const hasJoin = !!(o[id] && o[id].join);
    openFor(id, hasJoin && idx === 0 ? 'join' : 'task');
  });
});
chrome.notifications.onClosed.addListener(id => { if (id.startsWith('hlt-')) chrome.storage.session.remove(id); });

// The side panel can ask for an immediate check (e.g. after switching alerts on).
chrome.runtime.onMessage.addListener((msg, _s, send) => {
  if (msg && msg.type === 'hla-check-alerts') { checkAlerts().then(() => send({ ok: true })); return true; }
});
