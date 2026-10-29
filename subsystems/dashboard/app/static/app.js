// Delegated event handling for every dashboard and console page.
//
// The Content-Security-Policy forbids inline event handlers (onclick= and
// friends), so templates describe behaviour with data- attributes and this
// file wires them up:
//
//   data-onclick="fn" / data-onchange / data-oninput / data-onsubmit
//       call the page's global function fn with `this` = the element and
//       the arguments in data-args (a JSON array). An argument {"$": "this"},
//       {"$": "event"}, {"$": "value"} or {"$": "checked"} is replaced by the
//       element, the event, its value or its checked state. Returning false
//       cancels the event (as `return fn()` did inline).
//   data-onclick-self="fn"  as data-onclick, only for clicks on the element
//       itself (a modal's backdrop, not its contents)
//   data-confirm="message"  ask before a form submits or a button/link acts
//   data-busy="Saving…"     on a form: disable its first button and show this
//   data-copy="text"        copy text to the clipboard (button says Copied!)
//   data-show="id" / data-hide="id"  show (data-display, default flex) or
//       hide the element with that id
//   data-submit-on-change   submit the element's form when it changes
//   data-href="/path"       navigate there on click
(function () {
  'use strict';

  function arg(a, el, ev) {
    if (a && typeof a === 'object' && !Array.isArray(a) && Object.prototype.hasOwnProperty.call(a, '$')) {
      switch (a.$) {
        case 'this': return el;
        case 'event': return ev;
        case 'value': return el.value;
        case 'checked': return el.checked;
      }
    }
    return a;
  }

  function call(el, name, ev) {
    const fn = window[name];
    if (typeof fn !== 'function') {
      console.error('No page function named ' + name);
      return;
    }
    let args = [];
    if (el.dataset.args) {
      try { args = JSON.parse(el.dataset.args); } catch (e) { console.error('Bad data-args', el); }
    }
    if (fn.apply(el, args.map(a => arg(a, el, ev))) === false) ev.preventDefault();
  }

  function closest(ev, selector) {
    return ev.target && ev.target.closest ? ev.target.closest(selector) : null;
  }

  // Confirmations run first (capture), so a cancelled one stops everything.
  document.addEventListener('submit', ev => {
    const form = ev.target;
    if (form.dataset && form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      ev.preventDefault();
      ev.stopImmediatePropagation();
    }
  }, true);
  document.addEventListener('click', ev => {
    const el = closest(ev, '[data-confirm]:not(form)');
    if (el && !window.confirm(el.dataset.confirm)) {
      ev.preventDefault();
      ev.stopImmediatePropagation();
    }
  }, true);

  ['click', 'change', 'input', 'submit'].forEach(type => {
    document.addEventListener(type, ev => {
      const el = closest(ev, '[data-on' + type + ']');
      if (el) call(el, el.dataset['on' + type], ev);
    });
  });

  document.addEventListener('click', ev => {
    const self = closest(ev, '[data-onclick-self]');
    if (self && ev.target === self) call(self, self.dataset.onclickSelf, ev);

    const copy = closest(ev, '[data-copy]');
    if (copy) window.copyText(copy.dataset.copy, copy);

    const show = closest(ev, '[data-show]');
    if (show) {
      const t = document.getElementById(show.dataset.show);
      if (t) t.style.display = show.dataset.display || 'flex';
    }
    const hide = closest(ev, '[data-hide]');
    if (hide) {
      const t = document.getElementById(hide.dataset.hide);
      if (t) t.style.display = 'none';
    }

    const link = closest(ev, '[data-href]');
    if (link) window.location.href = link.dataset.href;
  });

  document.addEventListener('change', ev => {
    const el = closest(ev, '[data-submit-on-change]');
    if (el && el.form) el.form.submit();
  });

  document.addEventListener('submit', ev => {
    const form = ev.target;
    if (ev.defaultPrevented || !form.dataset || !form.dataset.busy) return;
    const button = form.querySelector('button');
    if (button) {
      button.disabled = true;
      button.textContent = form.dataset.busy;
    }
  });

  // Copy text to the clipboard; the button briefly says "Copied!".
  window.copyText = window.copyText || function (text, btn) {
    navigator.clipboard.writeText(text).then(() => {
      if (!btn) return;
      const prev = btn.innerHTML;
      btn.textContent = 'Copied!';
      btn.style.color = '#10B981';
      setTimeout(() => { btn.innerHTML = prev; btn.style.color = ''; }, 1500);
    });
  };
})();

// Phone layout for wide tables: every table with four or more columns gets
// class "stack", and each body cell a data-label naming its column, so
// style.css can show one card per row below 640px. Header text comes from
// the thead, or from a body row made only of <th> cells (tables built in
// JS). Opt a table out with data-nostack. Rows added later (filters,
// fetches) are labelled as they arrive.
(function () {
  'use strict';

  function headerRow(table) {
    if (table.tHead && table.tHead.rows.length) return table.tHead.rows[table.tHead.rows.length - 1];
    for (const body of table.tBodies) {
      for (const row of body.rows) {
        if (row.cells.length > 1 && Array.from(row.cells).every(c => c.tagName === 'TH')) return row;
      }
    }
    return null;
  }

  function label(table) {
    if (table.hasAttribute('data-nostack')) return;
    const head = headerRow(table);
    if (!head) return;
    const labels = [];
    for (const th of head.cells) {
      const text = th.textContent.replace(/[↕↑↓]/g, '').trim();
      for (let i = 0; i < th.colSpan; i++) labels.push(text);
    }
    if (labels.length < 4) return;
    table.classList.add('stack');
    head.classList.add('stack-head');
    for (const body of table.tBodies) {
      for (const row of body.rows) {
        if (row === head) continue;
        let i = 0;
        for (const cell of row.cells) {
          if (cell.colSpan > 1) cell.classList.add('stack-span');
          else if (labels[i]) cell.dataset.label = labels[i];
          i += cell.colSpan;
        }
      }
    }
  }

  let queued = false;
  function labelAll() {
    queued = false;
    document.querySelectorAll('table').forEach(label);
  }
  function queue() {
    if (!queued) { queued = true; requestAnimationFrame(labelAll); }
  }
  document.addEventListener('DOMContentLoaded', () => {
    labelAll();
    new MutationObserver(queue).observe(document.body, { childList: true, subtree: true });
  });
})();

// Timeline axes: hide any tick label that would overlap the one before it
// (phones show fewer dates instead of a smear). Re-checked on resize.
(function () {
  'use strict';
  function thin() {
    document.querySelectorAll('.tl-axis').forEach(axis => {
      const box = axis.getBoundingClientRect();
      let lastRight = -Infinity;
      axis.querySelectorAll('.tl-tick').forEach(tick => {
        tick.style.visibility = '';
        const r = tick.getBoundingClientRect();
        if (r.left < Math.max(lastRight + 6, box.left) || r.right > box.right) tick.style.visibility = 'hidden';
        else lastRight = r.right;
      });
    });
  }
  document.addEventListener('DOMContentLoaded', thin);
  window.addEventListener('resize', thin);
})();
