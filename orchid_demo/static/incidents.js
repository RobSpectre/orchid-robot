"use strict";
(() => {
  const list = document.getElementById('incident-list');
  const message = document.getElementById('incident-message');
  const delivery = document.getElementById('incident-delivery');
  const notes = new Map();
  let previous = '', busy = false;
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function fail(text) { message.textContent = text; message.hidden = !text; }
  async function json(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Diagnostic request failed. Try again.');
    return data;
  }
  function render(data) {
    delivery.textContent = data.delivery.enabled
      ? 'Report to Codex queues this incident for this chat. It is checked about once a minute while Codex is running.'
      : 'Reports are saved locally. Chat delivery is not configured; download diagnostics to share them in chat.';
    const key = JSON.stringify(data);
    if (key === previous || busy) return;
    previous = key;
    list.replaceChildren();
    if (!data.incidents.length) { list.append(element('p', 'No incidents recorded yet.', 'subtle')); return; }
    for (const item of data.incidents) {
      const article = element('article', '', 'incident');
      article.append(element('p', `${new Date(item.created).toLocaleString()} · ${item.mode} · ${item.id.slice(0, 8)}`, 'subtle'));
      const error = element('p', item.message, 'incident-error'); error.setAttribute('data-error-copy', '');
      article.append(error);
      if (item.imported) article.append(element('p', 'Recovered from earlier logs. Some diagnostic fields were not recorded by that app version.', 'subtle'));
      if (item.state !== 'saved') {
        article.append(element('p', item.state === 'save_failed' ? `Diagnostics could not be saved: ${item.save_error}` : 'Saving recent telemetry…'));
        list.append(article); continue;
      }
      const status = item.report?.state || 'not_requested';
      const labels = {not_requested: 'Diagnostics saved locally', queued: 'Queued for Codex', reviewing: 'Codex is reviewing', reviewed: 'Reviewed by Codex', needs_operator: 'Operator action needed'};
      const state = element('p', labels[status] || status, 'incident-status'); state.setAttribute('role', 'status');
      article.append(state);
      if (item.report?.summary) article.append(element('p', item.report.summary));
      const controls = element('div', '', 'incident-actions');
      const download = element('a', 'Download diagnostics', 'secondary');
      download.href = `/api/incidents/${item.id}`; download.download = `orchid-incident-${item.id}.json`;
      controls.append(download);
      if (status === 'not_requested') {
        const label = element('label', 'What happened? (optional)');
        const note = element('textarea'); note.rows = 2; note.maxLength = 1000; note.value = notes.get(item.id) || '';
        note.addEventListener('input', () => notes.set(item.id, note.value));
        label.append(note); article.append(label);
        const button = element('button', data.delivery.enabled ? 'Report to Codex' : 'Queue report', 'primary'); button.type = 'button';
        button.addEventListener('click', async () => {
          busy = true; button.disabled = true; button.textContent = 'Queueing…'; fail('');
          try {
            // A fresh CSRF token is sufficient. Never heartbeat, acquire a lease, or send a motor command.
            const session = await json('/api/session');
            await json(`/api/incidents/${item.id}/report`, {method:'POST', headers:{'Content-Type':'application/json','x-orchid-token':session.token}, body:JSON.stringify({note:note.value})});
            notes.delete(item.id); previous = '';
          } catch (err) { fail(`Report was not confirmed: ${err.message}`); button.disabled = false; button.textContent = 'Retry report'; }
          finally { busy = false; await poll(false); }
        });
        controls.append(button);
      }
      article.append(controls); list.append(article);
    }
  }
  async function poll(schedule = true) {
    try { render(await json('/api/incidents')); }
    catch { delivery.textContent = 'Incident history is unavailable. The app may need restarting to load diagnostic reporting.'; }
    finally { if (schedule) setTimeout(poll, 3000); }
  }
  poll();
})();
