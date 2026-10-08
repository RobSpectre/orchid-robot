"use strict";
(() => {
  // Browser-only diagnostics: no operator lease, fetch, or robot commands.
  const selector = '[data-error-copy], #error, #connection-warning, #connection-fault, #discovery-warnings, #discovery-status, #diagnostics-age, #leader-feedback-status';
  const conditional = {
    'discovery-status': 'Scan failed:',
    'diagnostics-age': 'Health read unavailable:',
    'leader-feedback-status': 'Stopped:'
  };
  const reports = new Map();

  async function writeClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch { /* Offer the browser's local fallback when clipboard permission is unavailable. */ }
    const previous = document.activeElement;
    const field = document.createElement('textarea');
    field.value = text;
    field.readOnly = true;
    field.className = 'clipboard-fallback';
    document.body.append(field);
    try {
      field.select();
      if (!document.execCommand('copy')) throw new Error('Clipboard unavailable');
    } finally {
      field.remove();
      previous?.focus({preventScroll: true});
    }
  }

  function attach(source) {
    const tools = document.createElement('div');
    tools.className = 'error-copy-tools';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'secondary copy-error';
    button.textContent = 'Copy error';
    const status = document.createElement('span');
    status.className = 'copy-error-status';
    status.setAttribute('role', 'status');
    const manual = document.createElement('textarea');
    manual.className = 'error-copy-manual';
    manual.readOnly = true;
    manual.hidden = true;
    manual.setAttribute('aria-label', 'Error text to copy manually');
    tools.append(button, status, manual);
    source.after(tools);
    const report = {tools, button, status, manual, text: '', copying: false};
    button.addEventListener('click', async () => {
      if (report.copying) return;
      const text = source.textContent;
      report.copying = true;
      button.disabled = true;
      try {
        await writeClipboard(text);
        if (source.isConnected && source.textContent === text) {
          button.textContent = 'Copied ✓';
          status.textContent = 'Error copied to clipboard.';
        }
      } catch {
        if (source.isConnected && source.textContent === text) {
          status.textContent = 'Clipboard blocked. Copy the selected text below.';
          manual.value = text;
          manual.hidden = false;
          manual.focus();
          manual.select();
        }
      } finally {
        report.copying = false;
        button.disabled = false;
      }
    });
    return report;
  }

  function refresh() {
    const active = new Set();
    for (const source of document.querySelectorAll(selector)) {
      const text = source.textContent;
      if (source.hidden || !text.trim() || (conditional[source.id] && !text.startsWith(conditional[source.id]))) continue;
      active.add(source);
      let report = reports.get(source);
      if (!report) { report = attach(source); reports.set(source, report); }
      if (report.text !== text) {
        report.text = text;
        report.button.textContent = 'Copy error';
        report.status.textContent = '';
        report.manual.hidden = true;
      }
    }
    for (const [source, report] of reports) {
      if (!active.has(source)) { report.tools.remove(); reports.delete(source); }
    }
  }
  let scheduled = false;
  new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => { scheduled = false; refresh(); });
  }).observe(document.body, {subtree: true, childList: true, characterData: true,
                            attributes: true, attributeFilter: ['hidden', 'data-error-copy']});
  refresh();
})();
