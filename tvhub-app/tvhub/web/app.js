/* tvhub - the whole admin front end.
 *
 * Every page loads this one file and dispatches on window.BOOT.page. The four
 * templates contain no script of their own beyond the BOOT assignment, which is
 * deliberate:
 *
 *  - Contract 10.2: no inline onclick, ever. Escaping a quoted handler out of
 *    Python, into an HTML attribute, into a JS string broke this page twice.
 *    Controls carry data-action / data-alias / data-arg and ONE delegated
 *    listener below reads them. Buttons built here are always type="button".
 *  - Both click and touchstart are bound. Without touchstart the UI is dead on
 *    a phone; with both, a guard stops every tap firing twice.
 *  - Nothing is ever written with innerHTML. Text goes in through textContent,
 *    so a photo filename or a TV label can never be read as markup.
 *
 * Written in plain ES5 (var, function, XMLHttpRequest) so it runs unchanged on
 * whatever tablet or phone browser is nearest, with no build step.
 */
(function () {
  'use strict';

  var BOOT = window.BOOT || {};
  var AV = BOOT.asset_version || '';
  var PAGE = BOOT.page || 'dashboard';

  /* ------------------------------------------------------------- helpers */

  function byId(id) { return document.getElementById(id); }
  function isArr(v) { return Object.prototype.toString.call(v) === '[object Array]'; }
  function attr(n, name) { return n && n.getAttribute ? n.getAttribute(name) : null; }

  function add(node, kids) {
    if (kids === null || kids === undefined || kids === false) { return node; }
    if (isArr(kids)) {
      for (var i = 0; i < kids.length; i++) { add(node, kids[i]); }
      return node;
    }
    if (typeof kids === 'string' || typeof kids === 'number') {
      node.appendChild(document.createTextNode(String(kids)));
      return node;
    }
    node.appendChild(kids);
    return node;
  }

  /* No 'html' option on purpose: there is no code path in this file that can
     put untrusted text through an HTML parser. */
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (!attrs.hasOwnProperty(k)) { continue; }
        var v = attrs[k];
        if (v === null || v === undefined) { continue; }
        if (k === 'text') { n.textContent = String(v); }
        else if (k === 'class') { n.className = String(v); }
        else if (k === 'disabled' || k === 'checked' || k === 'hidden') { if (v) { n[k] = true; n.setAttribute(k, k); } }
        else { n.setAttribute(k, String(v)); }
      }
    }
    return add(n, kids);
  }

  function clear(node) {
    if (!node) { return node; }
    while (node.firstChild) { node.removeChild(node.firstChild); }
    return node;
  }

  function setText(node, s) { if (node) { node.textContent = (s === null || s === undefined) ? '' : String(s); } }

  function show(node, on) {
    if (!node) { return; }
    if (on) { node.removeAttribute('hidden'); } else { node.setAttribute('hidden', 'hidden'); }
  }

  function closest(node, test) {
    while (node && node !== document) {
      if (node.nodeType === 1 && test(node)) { return node; }
      node = node.parentNode;
    }
    return null;
  }

  function hasAttrClosest(node, name) {
    return closest(node, function (n) { return n.hasAttribute && n.hasAttribute(name); });
  }

  function btn(label, action, data, cls) {
    var a = { 'class': 'btn ' + (cls || ''), 'type': 'button', 'data-action': action };
    if (data) {
      for (var k in data) {
        if (data.hasOwnProperty(k) && data[k] !== null && data[k] !== undefined) { a['data-' + k] = String(data[k]); }
      }
    }
    return el('button', a, label);
  }

  function link(label, href, cls) {
    return el('a', { 'class': 'btn ' + (cls || ''), href: href }, label);
  }

  function val(id) {
    var n = byId(id);
    if (!n) { return ''; }
    return (n.value === null || n.value === undefined) ? '' : String(n.value).trim();
  }

  function setVal(id, v) { var n = byId(id); if (n) { n.value = (v === null || v === undefined) ? '' : String(v); } }

  function num(v, dflt) {
    var f = parseFloat(v);
    return (v === '' || v === null || v === undefined || isNaN(f)) ? dflt : f;
  }

  function plural(n, one, many) { return String(n) + ' ' + (n === 1 ? one : many); }

  function fmtAge(sec) {
    if (sec === null || sec === undefined || sec === '' || isNaN(sec)) { return 'never'; }
    var s = Math.round(Number(sec));
    if (s < 0) { s = 0; }
    if (s < 60) { return s + 's ago'; }
    if (s < 3600) { return Math.round(s / 60) + 'm ago'; }
    return Math.round(s / 3600) + 'h ago';
  }

  function fmtBytes(b) {
    var n = Number(b);
    if (!n || n < 0 || isNaN(n)) { return '0 B'; }
    if (n < 1024) { return n + ' B'; }
    if (n < 1024 * 1024) { return (n / 1024).toFixed(0) + ' KB'; }
    return (n / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function fmtDuration(sec) {
    var s = Math.round(Number(sec) || 0);
    if (s < 90) { return s + 's'; }
    var m = Math.floor(s / 60);
    if (m < 90) { return m + 'm'; }
    var h = Math.floor(m / 60);
    if (h < 48) { return h + 'h ' + (m % 60) + 'm'; }
    return Math.floor(h / 24) + 'd ' + (h % 24) + 'h';
  }

  /* ----------------------------------------------------------------- api */

  function api(method, path, body, cb) {
    var x = new XMLHttpRequest();
    try { x.open(method, path, true); } catch (e) { if (cb) { cb(String(e), null); } return; }
    x.setRequestHeader('Accept', 'application/json');
    if (body !== null && body !== undefined) { x.setRequestHeader('Content-Type', 'application/json'); }
    x.onreadystatechange = function () {
      if (x.readyState !== 4) { return; }
      var data = null;
      if (x.responseText) {
        try { data = JSON.parse(x.responseText); } catch (e2) { data = null; }
      }
      if (x.status >= 200 && x.status < 300) { if (cb) { cb(null, data); } return; }
      var msg;
      if (!x.status) { msg = 'no answer from the server - is it still running?'; }
      else if (x.status === 403) { msg = 'forbidden - this address is not in admin_from in config.json'; }
      else { msg = (data && (data.error || data.message)) || ('HTTP ' + x.status); }
      if (cb) { cb(msg, data); }
    };
    try { x.send(body === null || body === undefined ? null : JSON.stringify(body)); }
    catch (e3) { if (cb) { cb(String(e3), null); } }
  }

  /* Multipart upload. The browser must set Content-Type itself so the boundary
     matches the body - never set it by hand here. Field name is "files",
     repeated once per file (contract 9.5). */
  function upload(playlist, files, onProgress, cb) {
    var fd;
    try { fd = new FormData(); } catch (e) { cb('this browser cannot upload files', null); return; }
    for (var i = 0; i < files.length; i++) { fd.append('files', files[i], files[i].name); }
    var x = new XMLHttpRequest();
    x.open('POST', '/api/playlists/' + encodeURIComponent(playlist) + '/images', true);
    x.setRequestHeader('Accept', 'application/json');
    if (x.upload && onProgress) {
      x.upload.onprogress = function (e) { if (e.lengthComputable) { onProgress(e.loaded / e.total); } };
    }
    x.onreadystatechange = function () {
      if (x.readyState !== 4) { return; }
      var data = null;
      if (x.responseText) { try { data = JSON.parse(x.responseText); } catch (e2) { data = null; } }
      if (x.status >= 200 && x.status < 300) { cb(null, data); return; }
      if (x.status === 413) { cb('the upload is bigger than the server limit (max_upload_mb)', data); return; }
      cb((data && (data.error || data.message)) || ('HTTP ' + (x.status || 0)), data);
    };
    x.send(fd);
  }

  /* --------------------------------------------------------------- toasts */

  function toastBox() {
    var b = byId('toasts');
    if (!b) {
      b = el('div', { id: 'toasts', 'class': 'toasts', 'aria-live': 'polite' });
      document.body.appendChild(b);
    }
    return b;
  }

  function toast(msg, level, ms) {
    var n = el('div', { 'class': 'toast ' + (level || '') });
    setText(n, msg);
    toastBox().appendChild(n);
    var timer = null;
    function close() { if (n.parentNode) { n.parentNode.removeChild(n); } }
    function arm(t) {
      if (timer) { clearTimeout(timer); timer = null; }
      if (t) { timer = setTimeout(close, t); }
    }
    arm(ms === undefined ? 6000 : ms);
    n.set = function (text, lvl, t) {
      setText(n, text);
      if (lvl !== undefined && lvl !== null) { n.className = 'toast ' + lvl; }
      arm(t);
    };
    n.close = close;
    return n;
  }

  /* The frozen text convention (contract 0.8): ok renders bare, a warning is
     prefixed WARNING, an error ERROR. Group replies are one "[alias] ..." line
     per TV, so strip that prefix before judging each line. */
  function levelOfText(s) {
    if (!s) { return 'ok'; }
    var lines = String(s).split('\n');
    var worst = 'ok';
    for (var i = 0; i < lines.length; i++) {
      var t = lines[i].replace(/^\s*\[[^\]]*\]\s*/, '');
      if (/^ERROR\b/.test(t)) { return 'error'; }
      if (/^WARNING\b/.test(t)) { worst = 'warn'; }
    }
    return worst;
  }

  function isOk(s) { return levelOfText(s) === 'ok'; }

  /* A job's `result` is whatever the action returned. Render every plausible
     shape rather than guessing one: a rendered string, a Result-ish object, a
     list of lines, or an alias->reply map from a group action.
     Structured payloads (the rows a scan returns) are summarised, not
     flattened - the page that asked for them renders them properly, and
     spilling every field into the job log buries the actual message. */
  function resultText(res) {
    if (res === null || res === undefined) { return ''; }
    if (typeof res === 'string') { return res; }
    if (typeof res === 'number' || typeof res === 'boolean') { return String(res); }
    if (isArr(res)) {
      var parts = [];
      var records = 0;
      for (var i = 0; i < res.length; i++) {
        if (res[i] && typeof res[i] === 'object' && typeof res[i].text !== 'string' &&
            typeof res[i].message !== 'string') {
          records++;
          continue;
        }
        var one = resultText(res[i]);
        if (one) { parts.push(one); }
      }
      if (records) { parts.push(plural(records, 'row', 'rows')); }
      return parts.join('\n');
    }
    if (typeof res === 'object') {
      if (typeof res.text === 'string') {
        var pre = '';
        /* Only add the prefix when the server handed us a raw Result rather
           than an already-rendered line - otherwise "ERROR ERROR ...". */
        if (!/^\s*(ERROR|WARNING)\b/.test(res.text)) {
          if (res.level === 'error') { pre = 'ERROR '; }
          else if (res.level === 'warn') { pre = 'WARNING '; }
        }
        return pre + res.text;
      }
      if (typeof res.message === 'string') { return res.message; }
      var lines = [];
      for (var k in res) {
        if (!res.hasOwnProperty(k)) { continue; }
        var v = res[k];
        if (isArr(v)) { lines.push('[' + k + '] ' + resultText(v)); continue; }
        if (v && typeof v === 'object' && typeof v.text !== 'string' && typeof v.message !== 'string') {
          continue;   /* nested data, not a message */
        }
        lines.push('[' + k + '] ' + resultText(v));
      }
      return lines.join('\n');
    }
    return String(res);
  }

  function jobProgress(job) {
    if (!job) { return ''; }
    var bits = [];
    if (job.step) { bits.push(String(job.step)); }
    if (job.total) { bits.push(String(job.done || 0) + '/' + String(job.total)); }
    return bits.join(' - ');
  }

  /* ----------------------------------------------------------------- jobs */

  function watchJob(id, opts) {
    opts = opts || {};
    var stopped = false;
    var interval = opts.interval || 1000;
    function tick() {
      if (stopped) { return; }
      api('GET', '/api/jobs/' + encodeURIComponent(id), null, function (err, job) {
        if (stopped) { return; }
        if (err) { if (opts.onDone) { opts.onDone(err, null); } return; }
        if (opts.onUpdate) { opts.onUpdate(job); }
        var state = job && job.state;
        if (state === 'done' || state === 'error') {
          if (opts.onDone) { opts.onDone(state === 'error' ? (job.error || 'the job failed') : null, job); }
          return;
        }
        setTimeout(tick, interval);
      });
    }
    setTimeout(tick, 250);
    return { cancel: function () { stopped = true; } };
  }

  function renderJobLog(node, job, extra) {
    if (!node) { return; }
    var out = [];
    var head = jobProgress(job);
    if (head) { out.push(head); }
    var lines = (job && job.lines) || [];
    for (var i = 0; i < lines.length; i++) { out.push(String(lines[i])); }
    if (extra) { out.push(extra); }
    setText(node, out.join('\n'));
    node.scrollTop = node.scrollHeight;
  }

  /* Start a job-returning POST and follow it to the end. `opts.log` writes
     progress into an element; otherwise a single toast is updated in place so
     the screen never fills with stale messages. */
  function runJob(method, path, body, title, opts) {
    opts = opts || {};
    var t = opts.log ? null : toast(title + '…', '', 0);
    if (opts.log) { setText(opts.log, title + '…'); show(opts.log, true); }
    api(method, path, body, function (err, data) {
      if (err) {
        if (t) { t.set(title + ': ' + err, 'error', 12000); }
        if (opts.log) { setText(opts.log, title + ': ' + err); }
        if (opts.done) { opts.done(err, null, err); }
        return;
      }
      var id = data && (data.job || data.id);
      if (!id) {
        /* Some routes answer synchronously with {ok,message}. */
        var msg = (data && (data.message || data.error)) || 'done';
        var lvl = (data && data.ok === false) ? 'error' : levelOfText(msg);
        if (t) { t.set(title + ': ' + msg, lvl, 8000); }
        if (opts.log) { setText(opts.log, msg); }
        if (opts.done) { opts.done(null, null, msg); }
        return;
      }
      if (data.started === false && opts.log) { setText(opts.log, 'already running - following that job'); }
      watchJob(id, {
        onUpdate: function (job) {
          var p = jobProgress(job) || 'working';
          if (t) { t.set(title + ' - ' + p, '', 0); }
          if (opts.log) { renderJobLog(opts.log, job); }
          if (opts.onUpdate) { opts.onUpdate(job); }
        },
        onDone: function (jerr, job) {
          var txt = jerr ? String(jerr) : resultText(job && job.result);
          if (!txt) { txt = jerr ? 'failed' : 'done'; }
          var lvl = jerr ? 'error' : levelOfText(txt);
          if (t) { t.set(title + ': ' + txt, lvl, lvl === 'ok' ? 7000 : 15000); }
          if (opts.log) { renderJobLog(opts.log, job, txt); }
          if (opts.done) { opts.done(jerr, job, txt); }
        }
      });
    });
  }

  function tvAction(alias, verb, arg, opts) {
    var p = '/api/tvs/' + encodeURIComponent(alias) + '/action/' + encodeURIComponent(verb);
    if (arg !== null && arg !== undefined && arg !== '') { p += '/' + encodeURIComponent(arg); }
    runJob('POST', p, {}, alias + ' ' + verb + (arg ? ' ' + arg : ''), opts);
  }

  function groupAction(name, verb, arg, opts) {
    var p = '/api/group/' + encodeURIComponent(name) + '/action/' + encodeURIComponent(verb);
    if (arg !== null && arg !== undefined && arg !== '') { p += '/' + encodeURIComponent(arg); }
    runJob('POST', p, {}, name + ' ' + verb + (arg ? ' ' + arg : ''), opts);
  }

  /* --------------------------------------------------- delegated listener */

  var ACTIONS = {};   /* data-action  -> fn(node, event)  */
  var CHANGES = {};   /* data-change  -> fn(node, event)  */
  var lastTouch = 0;

  function dataOf(node) {
    var d = {};
    var names = ['alias', 'arg', 'group', 'name', 'key', 'file', 'playlist', 'id', 'step', 'verb'];
    for (var i = 0; i < names.length; i++) {
      var v = attr(node, 'data-' + names[i]);
      if (v !== null) { d[names[i]] = v; }
    }
    return d;
  }

  function dispatch(ev) {
    var node = hasAttrClosest(ev.target, 'data-action');
    if (!node) { return; }
    if (ev.type === 'touchstart') {
      lastTouch = Date.now();
    } else if (Date.now() - lastTouch < 700) {
      /* The touch already ran this handler; the synthetic click follows ~300ms
         later and must be swallowed or every tap fires twice. */
      return;
    }
    if (node.disabled || attr(node, 'disabled') !== null || attr(node, 'aria-disabled') === 'true') { return; }
    var name = attr(node, 'data-action');
    var fn = ACTIONS[name];
    if (!fn) { return; }
    if (node.tagName !== 'A' && ev.type === 'click' && ev.preventDefault) { ev.preventDefault(); }
    try { fn(node, dataOf(node), ev); }
    catch (e) { toast('the page hit an error handling "' + name + '": ' + e, 'error', 12000); }
  }

  function dispatchChange(ev) {
    var node = hasAttrClosest(ev.target, 'data-change');
    if (!node) { return; }
    var fn = CHANGES[attr(node, 'data-change')];
    if (!fn) { return; }
    try { fn(node, dataOf(node), ev); }
    catch (e) { toast('the page hit an error: ' + e, 'error', 12000); }
  }

  /* data-enter="<action>" on a text field: Enter runs that action, which is how
     a phone keyboard's Go key behaves. */
  function dispatchKey(ev) {
    if (ev.keyCode !== 13 && ev.key !== 'Enter') { return; }
    var node = hasAttrClosest(ev.target, 'data-enter');
    if (!node) { return; }
    var fn = ACTIONS[attr(node, 'data-enter')];
    if (!fn) { return; }
    if (ev.preventDefault) { ev.preventDefault(); }
    try { fn(node, dataOf(node), ev); }
    catch (e) { toast('the page hit an error: ' + e, 'error', 12000); }
  }

  function bindDelegation() {
    document.addEventListener('touchstart', dispatch, false);
    document.addEventListener('click', dispatch, false);
    document.addEventListener('change', dispatchChange, false);
    document.addEventListener('keydown', dispatchKey, false);
  }

  /* --------------------------------------------------------- copy to clip */

  function copyText(s) {
    var done = function (ok) {
      toast(ok ? 'copied' : 'could not copy - select the text and copy it by hand',
        ok ? 'ok' : 'warn', 4000);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(s).then(function () { done(true); }, function () { done(false); });
      return;
    }
    var ta = el('textarea', { 'class': 'input', style: 'position:fixed;left:-2000px;top:0' });
    ta.value = s;
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    done(ok);
  }

  ACTIONS['copy'] = function (node, d) {
    var src = d.arg || '';
    if (!src && d.id) { var n = byId(d.id); src = n ? (n.value !== undefined && n.value !== '' ? n.value : n.textContent) : ''; }
    copyText(String(src || '').replace(/\s+$/, ''));
  };

  /* ------------------------------------------------------------- pollers */

  function poller(fn, ms) {
    var timer = null;
    var self = {};
    function run() {
      /* Skip the work while the tab is in the background - polling a hidden
         dashboard every 5s is pure noise on the server. */
      if (!document.hidden) { fn(); }
      timer = setTimeout(run, ms);
    }
    self.start = function () {
      if (timer === null) {
        /* The FIRST load always happens, hidden or not: a page opened in a
           background tab (or restored by a phone browser that reports hidden
           at load) must not sit empty until it happens to be focused. */
        fn();
        timer = setTimeout(run, ms);
      }
      return self;
    };
    self.stop = function () { if (timer !== null) { clearTimeout(timer); timer = null; } };
    self.kick = function () { fn(); };
    document.addEventListener('visibilitychange', function () {
      /* Coming back to the tab should show current data, not data from
         whenever it was last visible. */
      if (!document.hidden) { fn(); }
    }, false);
    return self;
  }

  /* ------------------------------------------------------- state helpers */

  var STATE_WORDS = {
    playing: 'playing',
    idle: 'idle',
    closed: 'closed',
    standby: 'standby',
    offline: 'offline',
    art: 'art mode',
    busy: 'busy'
  };

  function stateClass(s) {
    var k = String(s || '').toLowerCase();
    return STATE_WORDS[k] ? k : 'idle';
  }

  function stateBadge(row) {
    var s = stateClass(row && row.state);
    return el('span', { 'class': 'badge b-' + s, title: (row && row.detail) || '' }, STATE_WORDS[s] || s);
  }

  function metaBits(row) {
    var out = [];
    out.push(el('span', { 'class': 'mono' }, row.ip || ''));
    if (row.power) { out.push(el('span', {}, 'power ' + row.power)); }
    if (row.model) { out.push(el('span', {}, String(row.model))); }
    if (row.frame) { out.push(el('span', {}, 'Frame')); }
    out.push(el('span', {}, 'playlist ' + (row.playlist || '-')));
    if (row.state === 'playing' || (row.heartbeat_age !== null && row.heartbeat_age !== undefined)) {
      out.push(el('span', {}, 'page fetched ' + fmtAge(row.heartbeat_age)));
    } else {
      out.push(el('span', { 'class': 'warnflag' }, 'never fetched the page'));
    }
    if (row.paired) {
      out.push(el('span', {}, 'paired' + (row.verified_how ? ' (' + row.verified_how + ')' : '')));
    } else {
      out.push(el('span', { 'class': 'badflag' }, 'not paired'));
    }
    if (row.browser) { out.push(el('span', {}, 'browser ' + row.browser)); }
    if (row.identify_number) { out.push(el('span', {}, 'identify #' + row.identify_number)); }
    if (row.homepage_confirmed === false) { out.push(el('span', { 'class': 'warnflag' }, 'homepage not set')); }
    return out;
  }

  /* =====================================================================
   *  DASHBOARD
   * ===================================================================== */

  var DASH = { cards: {}, status: null, identify: false, playlists: [], routesLoaded: false };

  function dashCard(alias) {
    var f = {};
    f.name = el('span', { 'class': 'name' }, link('', '/ui/tv/' + encodeURIComponent(alias), 'plain'));
    var nameLink = f.name.firstChild;
    nameLink.className = '';
    f.nameLink = nameLink;
    f.alias = el('span', { 'class': 'alias' }, alias);
    f.badge = el('span', { 'class': 'badge' });
    f.meta = el('div', { 'class': 'meta' });
    f.busy = el('div', { 'class': 'busyline', hidden: true });
    f.acts = el('div', { 'class': 'acts' }, [
      btn('On', 'tv-act', { alias: alias, arg: 'on' }, 'small primary'),
      btn('Off', 'tv-act', { alias: alias, arg: 'off' }, 'small'),
      btn('Show', 'tv-act', { alias: alias, arg: 'show' }, 'small'),
      btn('Reopen', 'tv-act', { alias: alias, arg: 'reopen' }, 'small'),
      link('Open', '/ui/tv/' + encodeURIComponent(alias), 'small')
    ]);
    var card = el('div', { 'class': 'card' }, [
      el('div', { 'class': 'head' }, [f.name, f.alias, f.badge]),
      f.meta, f.busy, f.acts
    ]);
    card._f = f;
    return card;
  }

  function dashUpdateCard(card, row) {
    var f = card._f;
    setText(f.nameLink, row.label || row.alias);
    setText(f.alias, row.alias);
    var s = stateClass(row.state);
    card.className = 'card s-' + s + (row.enabled === false ? ' disabled' : '');
    f.badge.className = 'badge b-' + s;
    setText(f.badge, STATE_WORDS[s] || s);
    f.badge.setAttribute('title', row.detail || '');
    clear(f.meta);
    add(f.meta, metaBits(row));
    if (row.busy) {
      show(f.busy, true);
      card._busyText = row.detail || row.busy_text || 'working';
      var left = num(row.busy_left, 0);
      card._busyUntil = left > 0 ? (Date.now() + left * 1000) : 0;
      dashTickCard(card);
    } else {
      show(f.busy, false);
      card._busyUntil = 0;
    }
  }

  /* The server sends busy_left as of the moment it answered; ticking it down
     locally is what turns an unexplained pause into a visible countdown
     (contract 10.4). */
  function dashTickCard(card) {
    var f = card._f;
    if (!card._busyUntil) {
      if (card._busyText) { setText(f.busy, card._busyText); }
      return;
    }
    var left = Math.max(0, Math.round((card._busyUntil - Date.now()) / 1000));
    setText(f.busy, card._busyText + ' — ' + left + 's');
  }

  function dashRenderCards(rows) {
    var host = byId('cards');
    if (!host) { return; }
    var seen = {};
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      if (!row || !row.alias) { continue; }
      seen[row.alias] = true;
      var card = DASH.cards[row.alias];
      if (!card) {
        card = dashCard(row.alias);
        DASH.cards[row.alias] = card;
      }
      dashUpdateCard(card, row);
      /* Re-append in the server's order (busy, offline, idle, closed, standby,
         playing, then alias) so whatever needs attention stays at the top. */
      host.appendChild(card);
    }
    for (var a in DASH.cards) {
      if (DASH.cards.hasOwnProperty(a) && !seen[a]) {
        if (DASH.cards[a].parentNode) { DASH.cards[a].parentNode.removeChild(DASH.cards[a]); }
        delete DASH.cards[a];
      }
    }
    if (!rows.length) {
      host.appendChild(el('div', { 'class': 'note' }, [
        el('p', {}, 'No TVs are configured yet. '),
        link('Run setup', '/ui/setup', 'primary')
      ]));
    }
  }

  function dashRenderGroups(groups) {
    var host = byId('groups');
    if (!host) { return; }
    clear(host);
    var names = ['all'];
    for (var g in groups) { if (groups.hasOwnProperty(g) && g !== 'all') { names.push(g); } }
    for (var i = 0; i < names.length; i++) {
      var name = names[i];
      var members = groups[name];
      var sub = name === 'all' ? 'every enabled TV' : (isArr(members) ? plural(members.length, 'TV', 'TVs') : '');
      host.appendChild(el('div', { 'class': 'groupbar' }, [
        el('span', { 'class': 'gname' }, name),
        el('span', { 'class': 'gmembers' }, sub),
        el('span', { 'class': 'spacer' }),
        btn('On', 'group-act', { group: name, arg: 'on' }, 'small primary'),
        btn('Off', 'group-act', { group: name, arg: 'off' }, 'small'),
        btn('Show', 'group-act', { group: name, arg: 'show' }, 'small'),
        btn('Status', 'group-act', { group: name, arg: 'status' }, 'small')
      ]));
    }
  }

  function dashRenderPlaylists(pl) {
    var sel = byId('playlistpick');
    if (!sel) { return; }
    var avail = (pl && pl.available) || [];
    DASH.playlists = avail;
    var want = sel.value || (pl && pl.shared) || '';
    clear(sel);
    for (var i = 0; i < avail.length; i++) {
      var p = avail[i];
      var label = p.name + ' (' + plural(num(p.count, 0), 'image', 'images') + ')';
      sel.appendChild(el('option', { value: p.name }, label));
    }
    if (!avail.length) { sel.appendChild(el('option', { value: '' }, 'no playlists yet')); }
    sel.value = want || (pl && pl.shared) || '';
    if (!sel.value && avail.length) { sel.value = avail[0].name; }
    setText(byId('playlistmeta'), pl
      ? ('showing "' + (pl.shared || pl['default'] || '-') + '" on every TV that has no playlist of its own')
      : '');
  }

  function dashRenderServer(st) {
    var host = byId('serverline');
    if (!host) { return; }
    clear(host);
    var srv = st.server || {};
    var bits = [];
    bits.push(el('span', { 'class': 'strong' }, 'TVHub ' + (srv.version || '')));
    if (srv.base_url_set && srv.base_url) {
      bits.push(el('span', { 'class': 'mono' }, srv.base_url));
    } else {
      bits.push(el('span', { 'class': 'warnflag' }, 'server address not set'));
    }
    if (srv.uptime_seconds !== undefined && srv.uptime_seconds !== null) {
      bits.push(el('span', {}, 'up ' + fmtDuration(srv.uptime_seconds)));
    }
    if (st.age_seconds !== undefined && st.age_seconds !== null) {
      bits.push(el('span', {}, 'status ' + fmtAge(st.age_seconds)));
    }
    if (st.identify) { bits.push(el('span', { 'class': 'warnflag' }, 'identify is ON')); }
    host.appendChild(el('div', { 'class': 'meta' }, bits));
    var warns = srv.config_warnings || [];
    for (var i = 0; i < warns.length; i++) {
      host.appendChild(el('div', { 'class': 'note' }, String(warns[i])));
    }
  }

  function dashRenderSetup(st) {
    var host = byId('setupbanner');
    if (!host) { return; }
    var s = st.setup || {};
    if (!s.needs_setup) { show(host, false); return; }
    clear(host);
    show(host, true);
    var msg = 'Setup is not finished';
    if (s.next_step) { msg += ' - next: ' + s.next_step; }
    host.appendChild(el('div', { 'class': 'note' }, [
      el('p', {}, msg + '.'),
      el('div', { 'class': 'row' }, [link('Open setup', '/ui/setup', 'primary')])
    ]));
  }

  function dashRenderJobs(jobs) {
    var host = byId('jobstrip');
    if (!host) { return; }
    clear(host);
    jobs = jobs || [];
    var busy = 0;
    for (var i = 0; i < jobs.length; i++) {
      var j = jobs[i];
      if (j.state === 'running') { busy++; }
      var row = el('div', { 'class': 'jobrow ' + (j.state || '') }, [
        j.state === 'running' ? el('span', { 'class': 'spin' }) : null,
        el('span', { 'class': 'jstate' }, j.state || ''),
        el('span', {}, j.title || j.kind || j.id),
        el('span', { 'class': 'jstep' }, jobProgress(j))
      ]);
      host.appendChild(row);
    }
    if (!jobs.length) { host.appendChild(el('div', { 'class': 'hint tight' }, 'nothing running')); }
    setText(byId('jobcount'), busy ? String(busy) + ' running' : '');
  }

  function dashRefresh() {
    api('GET', '/api/status', null, function (err, st) {
      if (err) { setText(byId('serverline'), 'cannot reach the server: ' + err); return; }
      if (!st) { return; }
      DASH.status = st;
      DASH.identify = !!st.identify;
      dashRenderServer(st);
      dashRenderSetup(st);
      dashRenderPlaylists(st.playlist);
      dashRenderGroups(st.groups || {});
      dashRenderCards(st.tvs || []);
      dashRenderJobs(st.jobs);
      var ion = byId('identify-on');
      var ioff = byId('identify-off');
      if (ion) { ion.className = 'btn' + (DASH.identify ? ' on' : ''); }
      if (ioff) { ioff.className = 'btn' + (DASH.identify ? '' : ' on'); }
    });
  }

  function renderRoutes(host, data) {
    clear(host);
    var rows = [];
    if (isArr(data)) { rows = data; }
    else if (data && isArr(data.routes)) { rows = data.routes; }
    else if (data && typeof data === 'object') {
      for (var k in data) {
        if (data.hasOwnProperty(k)) { rows.push({ path: k, description: resultText(data[k]) }); }
      }
    }
    if (!rows.length) { host.appendChild(el('div', { 'class': 'hint' }, 'the server returned no route table')); return; }
    var body = el('tbody');
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (typeof r === 'string') { r = { path: r }; }
      var path = r.path || r.route || r.url || '';
      var abs = path.charAt(0) === '/' ? path : ('/' + path);
      body.appendChild(el('tr', {}, [
        el('td', { 'class': 'mono' }, (r.method || r.methods || 'GET')),
        el('td', {}, [el('a', { 'class': 'mono', href: abs }, path)]),
        el('td', {}, r.description || r.desc || r.note || '')
      ]));
    }
    host.appendChild(el('div', { 'class': 'tablewrap' }, [
      el('table', { 'class': 'tbl' }, [
        el('thead', {}, [el('tr', {}, [el('th', {}, 'method'), el('th', {}, 'path'), el('th', {}, 'what it does')])]),
        body
      ])
    ]));
  }

  function initDashboard() {
    ACTIONS['tv-act'] = function (n, d) { tvAction(d.alias, d.arg, null, { done: dashSoon }); };
    ACTIONS['group-act'] = function (n, d) { groupAction(d.group, d.arg, null, { done: dashSoon }); };
    ACTIONS['activate-playlist'] = function () {
      var name = val('playlistpick');
      if (!name) { toast('there are no playlists to choose from yet', 'warn'); return; }
      api('POST', '/api/playlists/' + encodeURIComponent(name) + '/activate', {}, function (err, data) {
        if (err) { toast('could not switch playlist: ' + err, 'error', 12000); return; }
        toast((data && data.message) || ('showing ' + name), (data && data.ok === false) ? 'error' : 'ok');
        dashRefresh();
      });
    };
    ACTIONS['identify'] = function (n, d) {
      var on = d.arg === 'on';
      api('POST', '/api/identify', { on: on }, function (err, data) {
        if (err) { toast('identify failed: ' + err, 'error', 12000); return; }
        toast((data && data.message) || ('identify ' + (on ? 'on' : 'off')), 'ok');
        dashRefresh();
      });
    };
    ACTIONS['heal'] = function () {
      runJob('POST', '/api/heal', {}, 'fixing stuck TVs', { done: dashSoon });
    };
    ACTIONS['refresh'] = function () { dashRefresh(); };

    var det = byId('routesbox');
    if (det) {
      det.addEventListener('toggle', function () {
        if (!det.open || DASH.routesLoaded) { return; }
        DASH.routesLoaded = true;
        var host = byId('routes');
        setText(host, 'loading…');
        api('GET', '/api/routes', null, function (err, data) {
          if (err) { setText(host, 'could not load the route table: ' + err); return; }
          renderRoutes(host, data);
        });
      }, false);
    }

    var p = poller(dashRefresh, 5000);
    p.start();
    DASH.poller = p;
  }

  function dashSoon() {
    /* An action changes a TV's state; refresh immediately and again once the
       status loop behind it has had a chance to re-probe. */
    if (DASH.poller) { DASH.poller.kick(); }
    setTimeout(function () { if (DASH.poller) { DASH.poller.kick(); } }, 2500);
  }

  /* =====================================================================
   *  SETUP WIZARD  (contract 10.5)
   * ===================================================================== */

  var SETUP = { setup: null, tvs: [], found: [], pairBusy: false, homepages: null, groups: {} };

  var STEPS = [
    { slug: 'server', id: 'step-server', re: /base|server|address|url/i },
    { slug: 'find', id: 'step-find', re: /discover|find|scan|add/i },
    { slug: 'pair', id: 'step-pair', re: /pair|token|verif/i },
    { slug: 'photos', id: 'step-photos', re: /photo|playlist|image|upload/i },
    { slug: 'homepage', id: 'step-homepage', re: /home/i },
    { slug: 'groups', id: 'step-groups', re: /group|macro/i }
  ];

  function stepDone(s) {
    var tvc = num(s.tv_count, 0);
    var conf = s.homepage_confirmed || {};
    var confirmed = 0;
    for (var k in conf) { if (conf.hasOwnProperty(k) && conf[k]) { confirmed++; } }
    return {
      server: !!s.base_url_set,
      find: tvc > 0,
      pair: tvc > 0 && num(s.paired_count, 0) >= tvc,
      photos: num(s.playlist_count, 0) > 0,
      homepage: tvc > 0 && confirmed >= tvc,
      groups: !!s.wizard_done
    };
  }

  function setupApplySteps(s) {
    var done = stepDone(s);
    var next = String(s.next_step || '');
    var finished = /^(done|complete|completed|finished|ok|none)$/i.test(next) || (!next && s.wizard_done);
    var current = null;
    if (!finished) {
      for (var i = 0; i < STEPS.length; i++) {
        if (next && STEPS[i].re.test(next)) { current = STEPS[i].slug; break; }
      }
      if (!current) {
        for (var j = 0; j < STEPS.length; j++) {
          if (!done[STEPS[j].slug]) { current = STEPS[j].slug; break; }
        }
      }
    }
    for (var k = 0; k < STEPS.length; k++) {
      var node = byId(STEPS[k].id);
      if (!node) { continue; }
      var cls = 'step';
      if (done[STEPS[k].slug]) { cls += ' done'; }
      if (STEPS[k].slug === current) { cls += ' now'; node.open = true; }
      node.className = cls;
      var tick = node.querySelector ? node.querySelector('summary .tick') : null;
      if (tick) {
        setText(tick, done[STEPS[k].slug] ? 'done' : (STEPS[k].slug === current ? 'do this next' : ''));
      }
    }
    show(byId('setupdone'), finished);
  }

  function setupRefresh(cb) {
    api('GET', '/api/setup', null, function (err, s) {
      if (err) { toast('cannot read setup state: ' + err, 'error', 12000); return; }
      SETUP.setup = s || {};
      setupApplySteps(SETUP.setup);
      if (cb) { cb(); }
    });
    api('GET', '/api/tvs', null, function (err, tvs) {
      if (err) { return; }
      SETUP.tvs = isArr(tvs) ? tvs : [];
      setupRenderPair();
      setupRenderHomepage();
      setupRenderGroupMembers();
      setupRenderFound();
    });
    api('GET', '/api/homepages', null, function (err, hp) {
      if (err) { return; }
      SETUP.homepages = hp || {};
      setupRenderHomepage();
      setupRenderServer();
    });
    api('GET', '/api/playlists', null, function (err, pl) {
      if (err) { return; }
      setupRenderPlaylists(pl || {});
    });
    api('GET', '/api/groups', null, function (err, g) {
      if (err) { return; }
      SETUP.groups = g || {};
      setupRenderGroupList();
    });
  }

  /* ---- step 1: server address ---- */

  function setupRenderServer() {
    var host = byId('addr-suggest');
    if (!host) { return; }
    clear(host);
    var seen = {};
    var cands = [];
    /* The address the admin is already using is always a valid candidate and
       needs no help from the server. */
    if (location.protocol === 'http:' || location.protocol === 'https:') {
      cands.push(location.protocol + '//' + location.host);
    }
    var ips = BOOT.local_ips || [];
    var port = (SETUP.setup && SETUP.setup.http_port) || location.port || '';
    for (var i = 0; i < ips.length; i++) {
      cands.push('http://' + ips[i] + (port ? ':' + port : ''));
    }
    for (var j = 0; j < cands.length; j++) {
      var c = cands[j].replace(/\/+$/, '');
      if (seen[c]) { continue; }
      seen[c] = true;
      host.appendChild(btn(c, 'use-addr', { arg: c }, 'small'));
    }
    var cur = (SETUP.homepages && SETUP.homepages.base_url) || BOOT.base_url || '';
    setText(byId('addr-current'), cur ? cur : 'not set yet');
    if (!val('addr-input')) { setVal('addr-input', cur); }
  }

  /* ---- step 2: find TVs ---- */

  function knownAliasFor(ip) {
    for (var i = 0; i < SETUP.tvs.length; i++) {
      if (SETUP.tvs[i] && SETUP.tvs[i].ip === ip) { return SETUP.tvs[i].alias; }
    }
    return '';
  }

  /* Aliases must match ^[a-z0-9][a-z0-9-]{0,31}$ and must not be one of
     RESERVED_NAMES (contract 2.1) - "tv" is reserved, so it can never be the
     fallback here however tempting it reads. */
  var RESERVED = ('all tv group api ui slideshow x health playlist playlists identify reload homepages').split(' ');

  function isReserved(name) {
    for (var i = 0; i < RESERVED.length; i++) { if (RESERVED[i] === name) { return true; } }
    return false;
  }

  function suggestAlias(row, used) {
    var base = String(row.name || row.model || '').toLowerCase();
    base = base.replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
    base = base.substring(0, 24).replace(/-+$/, '');
    if (!base || isReserved(base)) { base = base ? base + '-screen' : 'screen'; }
    var name = base;
    var n = 2;
    while (used[name]) { name = base + '-' + n; n++; }
    used[name] = true;
    return name;
  }

  function setupRenderFound() {
    var host = byId('found');
    if (!host) { return; }
    clear(host);
    if (!SETUP.found.length) { return; }
    var used = {};
    for (var u = 0; u < SETUP.tvs.length; u++) { if (SETUP.tvs[u]) { used[SETUP.tvs[u].alias] = true; } }
    var body = el('tbody');
    var anyNoHub = false;
    for (var i = 0; i < SETUP.found.length; i++) {
      var r = SETUP.found[i] || {};
      var known = r.alias || knownAliasFor(r.ip);
      var cells = [];
      cells.push(el('td', { 'class': 'mono' }, r.ip || ''));
      cells.push(el('td', {}, [
        el('div', {}, r.name || ''),
        el('div', { 'class': 'hint tight' }, [
          (r.model || ''),
          r.frame ? ' - Frame' : '',
          r.power ? ' - ' + r.power : ''
        ])
      ]));
      if (r.smart_hub === false) {
        anyNoHub = true;
        cells.push(el('td', { 'class': 'warnflag' }, 'not signed in'));
      } else {
        cells.push(el('td', {}, r.smart_hub === true ? 'signed in' : 'unknown'));
      }
      if (known) {
        cells.push(el('td', {}, [el('span', {}, 'added as '), el('span', { 'class': 'mono' }, known)]));
      } else {
        var inputId = 'alias-' + String(i);
        cells.push(el('td', {}, [
          el('div', { 'class': 'row tight' }, [
            el('input', {
              'class': 'input', id: inputId, value: suggestAlias(r, used),
              'data-enter': 'add-found', 'data-arg': String(i),
              autocapitalize: 'none', autocorrect: 'off', spellcheck: 'false'
            }),
            btn('Add', 'add-found', { arg: String(i) }, 'small primary')
          ])
        ]));
      }
      body.appendChild(el('tr', { 'class': r.smart_hub === false ? 'warnrow' : '' }, cells));
    }
    host.appendChild(el('div', { 'class': 'tablewrap' }, [
      el('table', { 'class': 'tbl' }, [
        el('thead', {}, [el('tr', {}, [
          el('th', {}, 'address'), el('th', {}, 'TV'), el('th', {}, 'Smart Hub'), el('th', {}, 'name it')
        ])]),
        body
      ])
    ]));
    if (anyNoHub) {
      host.appendChild(el('div', { 'class': 'note' },
        'Smart Hub is not signed in on at least one of these TVs. Until it is, the TV reports no apps at all and nothing can be launched. Sign in on the TV itself first.'));
    }
  }

  function setupTakeDiscovery(data) {
    var rows = [];
    if (isArr(data)) { rows = data; }
    else if (data && isArr(data.rows)) { rows = data.rows; }
    else if (data && isArr(data.found)) { rows = data.found; }
    else if (data && isArr(data.result)) { rows = data.result; }
    else if (data && data.result && isArr(data.result.rows)) { rows = data.result.rows; }
    SETUP.found = rows;
    setupRenderFound();
    return rows.length;
  }

  /* ---- step 3: pair ---- */

  function setupRenderPair() {
    var host = byId('pairlist');
    if (!host) { return; }
    clear(host);
    if (!SETUP.tvs.length) {
      host.appendChild(el('div', { 'class': 'hint' }, 'add a TV in step 2 first'));
      return;
    }
    for (var i = 0; i < SETUP.tvs.length; i++) {
      var tv = SETUP.tvs[i] || {};
      var ok = !!tv.paired;
      host.appendChild(el('div', { 'class': 'prow' + (ok ? ' active' : '') }, [
        el('div', {}, [
          el('div', { 'class': 'pname' }, tv.label || tv.alias),
          el('div', { 'class': 'pmeta mono' }, tv.ip || '')
        ]),
        el('span', { 'class': 'spacer' }),
        el('span', { 'class': ok ? 'pmeta' : 'warnflag' },
          ok ? ('paired' + (tv.verified_how ? ' and verified by ' + tv.verified_how : '')) : 'not paired'),
        btn(ok ? 'Pair again' : 'Pair', 'pair-tv', { alias: tv.alias }, ok ? 'small' : 'small primary'),
        btn('Verify', 'verify-tv', { alias: tv.alias }, 'small'),
        link('Open', '/ui/tv/' + encodeURIComponent(tv.alias), 'small')
      ]));
    }
    var buttons = host.querySelectorAll ? host.querySelectorAll('button[data-action="pair-tv"]') : [];
    for (var b = 0; b < buttons.length; b++) {
      if (SETUP.pairBusy) { buttons[b].disabled = true; } else { buttons[b].disabled = false; }
    }
  }

  /* ---- step 4: photos ---- */

  function setupRenderPlaylists(pl) {
    var sel = byId('setup-playlist');
    if (!sel) { return; }
    var list = (pl && pl.playlists) || [];
    var want = sel.value || (pl && pl.shared) || '';
    clear(sel);
    for (var i = 0; i < list.length; i++) {
      sel.appendChild(el('option', { value: list[i].name },
        list[i].name + ' (' + plural(num(list[i].count, 0), 'image', 'images') + ')'));
    }
    if (!list.length) { sel.appendChild(el('option', { value: '' }, 'no playlists yet')); }
    if (want) { sel.value = want; }
    if (!sel.value && list.length) { sel.value = list[0].name; }
  }

  /* ---- step 5: homepage ---- */

  function setupRenderHomepage() {
    var hp = SETUP.homepages || {};
    var url = hp.homepage_url || BOOT.homepage_url || '';
    setText(byId('homepage-url'), url || 'set the server address in step 1 first');
    var cbtn = byId('homepage-copy');
    if (cbtn) { cbtn.setAttribute('data-arg', url); }
    if (hp.base_url_set === false) {
      show(byId('homepage-warn'), true);
    } else {
      show(byId('homepage-warn'), false);
    }
    var ins = byId('homepage-steps');
    if (ins) {
      clear(ins);
      var lines = isArr(hp.instructions) && hp.instructions.length ? hp.instructions : [
        'On the TV, open the Internet (browser) app with the remote.',
        'Go to the address above.',
        'Open the browser menu and set it as the homepage / start page.',
        'Do this once per TV. The address never changes.'
      ];
      for (var i = 0; i < lines.length; i++) { ins.appendChild(el('li', {}, String(lines[i]))); }
    }
    var host = byId('homepagelist');
    if (!host) { return; }
    clear(host);
    if (!SETUP.tvs.length) {
      host.appendChild(el('div', { 'class': 'hint' }, 'add a TV in step 2 first'));
      return;
    }
    var per = hp.per_tv || {};
    for (var t = 0; t < SETUP.tvs.length; t++) {
      var tv = SETUP.tvs[t] || {};
      var own = per[tv.alias];
      var resId = 'hptest-' + tv.alias;
      var box = el('input', {
        type: 'checkbox', 'data-change': 'homepage-confirm', 'data-alias': tv.alias
      });
      if (tv.homepage_confirmed) { box.checked = true; }
      host.appendChild(el('div', { 'class': 'prow' }, [
        el('div', {}, [
          el('div', { 'class': 'pname' }, tv.label || tv.alias),
          own && own !== url ? el('div', { 'class': 'pmeta mono' }, String(own)) : null
        ]),
        el('span', { 'class': 'spacer' }),
        el('label', { 'class': 'chk' }, [box, 'homepage set']),
        btn('Open Internet', 'open-tv-browser', { alias: tv.alias }, 'small'),
        btn('Test', 'test-tv', { alias: tv.alias }, 'small primary'),
        link('Macro', '/ui/tv/' + encodeURIComponent(tv.alias), 'small'),
        el('div', { 'class': 'hint tight', id: resId, style: 'flex:1 1 100%' })
      ]));
    }
  }

  /* ---- step 6: groups ---- */

  function setupRenderGroupMembers() {
    var host = byId('group-members');
    if (!host) { return; }
    clear(host);
    for (var i = 0; i < SETUP.tvs.length; i++) {
      var tv = SETUP.tvs[i] || {};
      host.appendChild(el('label', { 'class': 'chk' }, [
        el('input', { type: 'checkbox', 'class': 'gmember', value: tv.alias }),
        (tv.label || tv.alias)
      ]));
    }
    if (!SETUP.tvs.length) { host.appendChild(el('div', { 'class': 'hint' }, 'no TVs yet')); }
  }

  function setupRenderGroupList() {
    var host = byId('grouplist');
    if (!host) { return; }
    clear(host);
    var any = false;
    for (var g in SETUP.groups) {
      if (!SETUP.groups.hasOwnProperty(g)) { continue; }
      any = true;
      var members = SETUP.groups[g] || [];
      host.appendChild(el('div', { 'class': 'prow' }, [
        el('div', {}, [
          el('div', { 'class': 'pname' }, g),
          el('div', { 'class': 'pmeta' }, members.join(', '))
        ]),
        el('span', { 'class': 'spacer' }),
        btn('Load', 'group-load', { group: g }, 'small'),
        btn('Delete', 'group-del', { group: g }, 'small danger')
      ]));
    }
    if (!any) { host.appendChild(el('div', { 'class': 'hint' }, 'no groups - they are optional')); }
  }

  function initSetup() {
    ACTIONS['use-addr'] = function (n, d) { setVal('addr-input', d.arg); };
    ACTIONS['save-addr'] = function () {
      var v = val('addr-input').replace(/\/+$/, '');
      if (!/^https?:\/\/[^\/\s]+$/.test(v)) {
        toast('that does not look like an address - it should be http://host or http://host:port', 'warn', 10000);
        return;
      }
      api('POST', '/api/setup', { base_url: v }, function (err, data) {
        if (err) { toast('could not save the address: ' + err, 'error', 12000); return; }
        toast((data && data.message) || ('server address set to ' + v), 'ok');
        setupRefresh(setupRenderServer);
      });
    };

    /* A scan only sees TVs that are powered on and already on this network, so
       "nothing new" usually means a set still needs connecting rather than that
       the fleet is complete. Open the how-to-connect panel instead of leaving
       an empty list and no next step. */
    function offerOfflineHelp() {
      var help = byId('tv-offline-help');
      if (!help) { return; }
      var found = (SETUP && SETUP.found) ? SETUP.found : [];
      var fresh = 0;
      for (var i = 0; i < found.length; i++) {
        if (!found[i].alias) { fresh++; }
      }
      if (fresh === 0) {
        help.open = true;
        if (help.scrollIntoView) { help.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
      }
    }

    ACTIONS['discover'] = function () {
      var cidr = val('cidr');
      var body = cidr ? { cidr: cidr } : {};
      runJob('POST', '/api/discover', body, 'looking for TVs', {
        log: byId('discover-log'),
        onUpdate: function (job) { if (job && job.result) { setupTakeDiscovery(job.result); } },
        done: function (err, job) {
          if (job && job.result && setupTakeDiscovery(job.result)) { offerOfflineHelp(); return; }
          api('GET', '/api/discover', null, function (e2, data) {
            if (!e2) { setupTakeDiscovery(data); }
            offerOfflineHelp();
          });
        }
      });
    };
    ACTIONS['add-found'] = function (n, d) {
      var idx = parseInt(d.arg, 10);
      var row = SETUP.found[idx];
      if (!row) { return; }
      var alias = val('alias-' + d.arg);
      if (!alias) { toast('give the TV a short name first', 'warn'); return; }
      api('POST', '/api/tvs', { alias: alias, ip: row.ip, mac: row.mac || '', label: row.name || alias },
        function (err, data) {
          if (err) { toast('could not add that TV: ' + err, 'error', 12000); return; }
          toast((data && data.message) || ('added ' + alias), 'ok');
          setupRefresh();
        });
    };
    ACTIONS['add-manual'] = function () {
      var alias = val('m-alias');
      var ip = val('m-ip');
      if (!alias || !ip) { toast('a short name and an address are both needed', 'warn'); return; }
      api('POST', '/api/tvs', { alias: alias, ip: ip, mac: val('m-mac'), label: val('m-label') || alias },
        function (err, data) {
          if (err) { toast('could not add that TV: ' + err, 'error', 12000); return; }
          toast((data && data.message) || ('added ' + alias), 'ok');
          setVal('m-alias', ''); setVal('m-ip', ''); setVal('m-mac', ''); setVal('m-label', '');
          setupRefresh();
        });
    };

    /* One TV at a time (contract 10.5 step 3): the TV shows a modal ALLOW box
       and a human has to walk to it, so parallel pairing just times out. */
    ACTIONS['pair-tv'] = function (n, d) {
      if (SETUP.pairBusy) { toast('finish the pairing that is already running first', 'warn'); return; }
      SETUP.pairBusy = true;
      setupRenderPair();
      var logNode = byId('pair-log');
      show(logNode, true);
      setText(logNode, 'Turn ' + d.alias + ' on, then choose ALLOW on the TV screen. Waiting up to 90 seconds…');
      runJob('POST', '/api/tvs/' + encodeURIComponent(d.alias) + '/pair', { wait: 90 },
        'pairing ' + d.alias, {
          log: logNode,
          done: function (err, job, txt) {
            SETUP.pairBusy = false;
            /* "Paired" is only claimed when the reply says the pairing was
               verified by effect - a stored token is not proof of pairing
               (invariant I1), so neither is a tidy-looking reply. */
            if (!err && txt && isOk(txt) && /verif/i.test(txt)) {
              toast(d.alias + ': paired and verified', 'ok', 8000);
            } else if (!err && txt && isOk(txt)) {
              toast(d.alias + ': ' + txt + ' (not reported as verified - press Verify)', 'warn', 15000);
            } else {
              toast(d.alias + ': ' + (err || txt || 'pairing did not complete'), 'error', 15000);
            }
            setupRefresh();
          }
        });
    };
    ACTIONS['verify-tv'] = function (n, d) {
      runJob('POST', '/api/tvs/' + encodeURIComponent(d.alias) + '/verify', {},
        'verifying ' + d.alias, { log: byId('pair-log'), done: function () { setupRefresh(); } });
    };

    ACTIONS['make-playlist'] = function () {
      var name = val('new-playlist');
      if (!name) { toast('type a name for the playlist', 'warn'); return; }
      api('POST', '/api/playlists', { name: name }, function (err, data) {
        if (err) { toast('could not create it: ' + err, 'error', 12000); return; }
        toast((data && data.message) || ('created ' + name), 'ok');
        setVal('new-playlist', '');
        setupRefresh();
      });
    };
    ACTIONS['setup-upload'] = function () {
      doUpload(val('setup-playlist'), byId('setup-files'), byId('setup-upbar'), byId('setup-rejects'),
        function () { setupRefresh(); });
    };

    /* Opens the Internet app on that TV so the user does not have to hunt for it
     with the remote before typing the homepage address. Firmware that refuses
     this is exactly the firmware that needs an open macro, so say that rather
     than failing quietly. */
  /* The per-TV steps (find, sign in, pair, homepage) are one TV's worth of work
     and get repeated. These two just steer the accordion so the loop is obvious
     instead of the user guessing whether to scroll back up. */
  ACTIONS['add-another-tv'] = function () {
    var tvs = byId('step-find');
    if (tvs) {
      tvs.open = true;
      if (tvs.scrollIntoView) { tvs.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
      var ip = byId('m-alias');
      if (ip && ip.focus) { setTimeout(function () { ip.focus(); }, 350); }
    }
  };
  ACTIONS['tvs-done'] = function () {
    var photos = byId('step-photos');
    if (photos) {
      photos.open = true;
      if (photos.scrollIntoView) { photos.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    }
  };

  ACTIONS['open-tv-browser'] = function (n, d) {
    var out = byId('hptest-' + d.alias);
    if (out) { out.className = 'hint tight'; setText(out, 'asking ' + d.alias + ' to open its browser…'); }
    tvAction(d.alias, 'reopen', null, {
      done: function (err, job, txt) {
        if (!out) { return; }
        if (err) {
          out.className = 'note bad';
          setText(out, (err || 'no answer') +
            '  |  This TV will not open its browser on request - record an open macro on its page.');
          return;
        }
        out.className = 'hint tight';
        setText(out, String(txt || 'asked') +
          ' - now type the address above into its browser and save it as the homepage.');
      }
    });
  };

  ACTIONS['test-tv'] = function (n, d) {
      var out = byId('hptest-' + d.alias);
      if (out) { setText(out, 'asking the TV to show the slideshow…'); }
      tvAction(d.alias, 'show', null, {
        done: function (err, job, txt) {
          if (!out) { return; }
          if (!err && txt && isOk(txt)) {
            out.className = 'hint tight';
            setText(out, 'working - the TV fetched the page. ' + txt);
          } else {
            out.className = 'note bad';
            setText(out, (err || txt || 'no answer') +
              '  |  If the TV never fetches the page, set the homepage by hand as above, then record an open macro on the TV page.');
          }
          setupRefresh();
        }
      });
    };
    CHANGES['homepage-confirm'] = function (n, d) {
      api('POST', '/api/tvs/' + encodeURIComponent(d.alias) + '/homepage', { confirmed: !!n.checked },
        function (err) {
          if (err) { toast('could not record that: ' + err, 'error', 12000); n.checked = !n.checked; return; }
          setupRefresh();
        });
    };

    ACTIONS['group-save'] = function () {
      var name = val('group-name');
      if (!name) { toast('name the group first', 'warn'); return; }
      var boxes = document.querySelectorAll('#group-members input.gmember');
      var aliases = [];
      for (var i = 0; i < boxes.length; i++) { if (boxes[i].checked) { aliases.push(boxes[i].value); } }
      if (!aliases.length) { toast('tick at least one TV', 'warn'); return; }
      api('PUT', '/api/groups/' + encodeURIComponent(name), { aliases: aliases }, function (err, data) {
        if (err) { toast('could not save the group: ' + err, 'error', 12000); return; }
        toast((data && data.message) || ('saved group ' + name), 'ok');
        setupRefresh();
      });
    };
    ACTIONS['group-load'] = function (n, d) {
      setVal('group-name', d.group);
      var members = SETUP.groups[d.group] || [];
      var boxes = document.querySelectorAll('#group-members input.gmember');
      for (var i = 0; i < boxes.length; i++) {
        boxes[i].checked = false;
        for (var j = 0; j < members.length; j++) { if (members[j] === boxes[i].value) { boxes[i].checked = true; } }
      }
    };
    ACTIONS['group-del'] = function (n, d) {
      if (!window.confirm('Delete the group "' + d.group + '"? The TVs themselves are not touched.')) { return; }
      api('DELETE', '/api/groups/' + encodeURIComponent(d.group), null, function (err) {
        if (err) { toast('could not delete it: ' + err, 'error', 12000); return; }
        toast('deleted group ' + d.group, 'ok');
        setupRefresh();
      });
    };
    ACTIONS['finish-setup'] = function () {
      api('POST', '/api/setup', { wizard_done: true }, function (err) {
        if (err) { toast('could not save: ' + err, 'error', 12000); return; }
        toast('setup marked finished', 'ok');
        setupRefresh();
      });
    };
    ACTIONS['reopen-wizard'] = function () {
      api('POST', '/api/setup', { wizard_done: false }, function () { setupRefresh(); });
    };

    setupRefresh(setupRenderServer);
    setTimeout(setupRenderServer, 400);
  }

  /* =====================================================================
   *  PHOTOS  (contract 10.8)
   * ===================================================================== */

  var PHOTOS = { sel: '', shared: '', list: [], perTv: {} };

  function photosRenderList() {
    var host = byId('plist');
    if (!host) { return; }
    clear(host);
    if (!PHOTOS.list.length) {
      host.appendChild(el('div', { 'class': 'hint' }, 'no playlists yet - create one above, then add photos'));
      return;
    }
    for (var i = 0; i < PHOTOS.list.length; i++) {
      var p = PHOTOS.list[i];
      var active = p.name === PHOTOS.shared;
      /* Only the shared pointer can be identified locally with certainty, so
         only that one is blocked here (10.8). A playlist pinned to one TV is
         merely warned about: the server owns that refusal (8.6) and a resolved
         playlist is not necessarily a stored pointer. */
      var pinned = false;
      for (var a in PHOTOS.perTv) {
        if (PHOTOS.perTv.hasOwnProperty(a) && PHOTOS.perTv[a] === p.name && !active) { pinned = true; }
      }
      host.appendChild(el('div', {
        'class': 'prow' + (active ? ' active' : '') + (p.name === PHOTOS.sel ? ' sel' : '')
      }, [
        el('div', {}, [
          el('div', { 'class': 'pname' }, p.name + (active ? '  • on every TV' : '')),
          el('div', { 'class': 'pmeta' }, [
            plural(num(p.count, 0), 'image', 'images'),
            p.bytes ? '  •  ' + fmtBytes(p.bytes) : ''
          ])
        ]),
        el('span', { 'class': 'spacer' }),
        btn('Photos', 'pick-playlist', { name: p.name }, 'small'),
        btn(active ? 'Showing' : 'Show on all TVs', 'activate', { name: p.name }, active ? 'small' : 'small primary'),
        btn('Delete', 'del-playlist', { name: p.name, arg: active ? 'active' : (pinned ? 'pinned' : '') }, 'small danger')
      ]));
    }
  }

  function photosRenderThumbs(data) {
    var host = byId('thumbs');
    if (!host) { return; }
    clear(host);
    var imgs = (data && data.images) || [];
    setText(byId('thumbhead'), PHOTOS.sel
      ? (PHOTOS.sel + ' - ' + plural(imgs.length, 'image', 'images'))
      : 'pick a playlist');
    if (!imgs.length) {
      host.appendChild(el('div', { 'class': 'hint' }, 'nothing in this playlist yet'));
      return;
    }
    for (var i = 0; i < imgs.length; i++) {
      var im = imgs[i];
      var fn = im.filename || im.name || String(im);
      var url = im.url || ('/slideshow/p/' + encodeURIComponent(PHOTOS.sel) + '/img/' + encodeURIComponent(fn));
      host.appendChild(el('div', { 'class': 'thumb' }, [
        el('img', { src: url, alt: fn, loading: 'lazy' }),
        el('div', { 'class': 'fn', title: fn }, fn),
        btn('✕', 'del-image', { name: PHOTOS.sel, file: fn }, 'del small')
      ]));
    }
  }

  function photosLoadThumbs() {
    if (!PHOTOS.sel) { photosRenderThumbs(null); return; }
    api('GET', '/api/playlists/' + encodeURIComponent(PHOTOS.sel) + '/images', null, function (err, data) {
      if (err) { setText(byId('thumbhead'), 'could not list images: ' + err); return; }
      photosRenderThumbs(data);
    });
  }

  function photosRefresh() {
    api('GET', '/api/playlists', null, function (err, data) {
      if (err) { toast('cannot list playlists: ' + err, 'error', 12000); return; }
      PHOTOS.list = (data && data.playlists) || [];
      PHOTOS.shared = (data && data.shared) || '';
      if (!PHOTOS.sel) { PHOTOS.sel = PHOTOS.shared || (PHOTOS.list.length ? PHOTOS.list[0].name : ''); }
      photosRenderList();
      photosLoadThumbs();
    });
    /* Which playlists are pinned to a single TV matters here: deleting one of
       those is refused by the server, and saying so up front is kinder. */
    api('GET', '/api/status', null, function (err, st) {
      if (err || !st) { return; }
      var map = {};
      var rows = st.tvs || [];
      for (var i = 0; i < rows.length; i++) { if (rows[i].playlist) { map[rows[i].alias] = rows[i].playlist; } }
      PHOTOS.perTv = map;
      photosRenderList();
    });
  }

  function doUpload(playlist, fileInput, bar, rejectHost, after) {
    if (!playlist) { toast('pick a playlist first', 'warn'); return; }
    if (!fileInput || !fileInput.files || !fileInput.files.length) { toast('choose some image files first', 'warn'); return; }
    var files = fileInput.files;
    if (rejectHost) { clear(rejectHost); }
    if (bar) { show(bar, true); var fill = bar.firstChild; if (fill) { fill.style.width = '0%'; } }
    var t = toast('uploading ' + plural(files.length, 'file', 'files') + '…', '', 0);
    upload(playlist, files, function (frac) {
      if (bar && bar.firstChild) { bar.firstChild.style.width = Math.round(frac * 100) + '%'; }
      t.set('uploading — ' + Math.round(frac * 100) + '%', '', 0);
    }, function (err, data) {
      if (bar) { show(bar, false); }
      if (err) { t.set('upload failed: ' + err, 'error', 15000); return; }
      var added = (data && data.added) || [];
      var rejected = (data && data.rejected) || [];
      var n = (data && data.count !== undefined) ? data.count : added.length;
      t.set('added ' + plural(n, 'image', 'images') +
        (rejected.length ? ', ' + plural(rejected.length, 'file was', 'files were') + ' rejected' : ''),
        rejected.length ? 'warn' : 'ok', 9000);
      if (rejectHost) {
        for (var i = 0; i < rejected.length; i++) {
          var r = rejected[i] || {};
          rejectHost.appendChild(el('div', { 'class': 'note bad' }, [
            el('span', { 'class': 'strong' }, (r.name || 'that file') + ': '),
            el('span', {}, r.message || r.reason || 'rejected')
          ]));
        }
        /* classify_upload attaches size advice to files it accepted; show it,
           because a 40MP photo works but takes an age to paint on a TV. */
        for (var j = 0; j < added.length; j++) {
          var adv = added[j] && added[j].advice;
          if (adv) { rejectHost.appendChild(el('div', { 'class': 'note' }, String(adv))); }
        }
      }
      if (fileInput) { try { fileInput.value = ''; } catch (e) { /* IE-ish; harmless */ } }
      if (after) { after(); }
    });
  }

  function initPhotos() {
    ACTIONS['pick-playlist'] = function (n, d) {
      PHOTOS.sel = d.name;
      photosRenderList();
      photosLoadThumbs();
    };
    ACTIONS['activate'] = function (n, d) {
      api('POST', '/api/playlists/' + encodeURIComponent(d.name) + '/activate', {}, function (err, data) {
        if (err) { toast('could not switch: ' + err, 'error', 12000); return; }
        toast((data && data.message) || ('now showing ' + d.name), 'ok');
        photosRefresh();
      });
    };
    ACTIONS['new-playlist'] = function () {
      var name = val('newname');
      if (!name) { toast('type a name first', 'warn'); return; }
      api('POST', '/api/playlists', { name: name }, function (err, data) {
        if (err) { toast('could not create it: ' + err, 'error', 12000); return; }
        toast((data && data.message) || ('created ' + name), 'ok');
        setVal('newname', '');
        PHOTOS.sel = name;
        photosRefresh();
      });
    };
    ACTIONS['del-playlist'] = function (n, d) {
      if (d.arg === 'active') {
        toast('"' + d.name + '" is the playlist the TVs are showing right now. Switch every TV to another playlist first, then delete it.', 'warn', 12000);
        return;
      }
      var extra = d.arg === 'pinned'
        ? '\n\nA TV is currently set to this playlist, so the server may refuse.'
        : '';
      if (!window.confirm('Delete the playlist "' + d.name + '" and every photo in it?' + extra)) { return; }
      api('DELETE', '/api/playlists/' + encodeURIComponent(d.name), null, function (err, data) {
        if (err) { toast('could not delete it: ' + err, 'error', 14000); return; }
        var msg = (data && data.message) || ('deleted ' + d.name);
        toast(msg, (data && data.ok === false) ? 'error' : 'ok', 9000);
        if (PHOTOS.sel === d.name) { PHOTOS.sel = ''; }
        photosRefresh();
      });
    };
    ACTIONS['del-image'] = function (n, d) {
      if (!window.confirm('Delete ' + d.file + '?')) { return; }
      api('DELETE', '/api/playlists/' + encodeURIComponent(d.name) + '/images/' + encodeURIComponent(d.file),
        null, function (err) {
          if (err) { toast('could not delete it: ' + err, 'error', 12000); return; }
          photosRefresh();
        });
    };
    ACTIONS['upload'] = function () {
      doUpload(PHOTOS.sel, byId('files'), byId('upbar'), byId('rejects'), photosRefresh);
    };
    photosRefresh();
  }

  /* =====================================================================
   *  PER-TV PAGE: remote, macro recorder, options  (contract 10.6 / 10.7)
   * ===================================================================== */

  var ALIAS = BOOT.alias || decodeURIComponent(String(location.pathname).replace(/\/+$/, '').split('/').pop() || '');
  var TVP = { tv: {}, row: {}, rec: false, seq: [], homepage: '' };

  /* Key names are the raw Samsung remote codes; the grammar (2.2) upper-cases a
     bare name and prefixes KEY_, and the server applies the inter-key gap. */
  var PAD = [
    [{ k: 'KEY_POWER', t: 'Power' }, { k: 'KEY_SOURCE', t: 'Source' }, { k: 'KEY_HOME', t: 'Home' }],
    [{ k: '', t: '' }, { k: 'KEY_UP', t: '▲' }, { k: '', t: '' }],
    [{ k: 'KEY_LEFT', t: '◀' }, { k: 'KEY_ENTER', t: 'OK' }, { k: 'KEY_RIGHT', t: '▶' }],
    [{ k: '', t: '' }, { k: 'KEY_DOWN', t: '▼' }, { k: '', t: '' }],
    [{ k: 'KEY_RETURN', t: 'Back' }, { k: 'KEY_EXIT', t: 'Exit' }, { k: 'KEY_TOOLS', t: 'Tools' }],
    [{ k: 'KEY_VOLUP', t: 'Vol +' }, { k: 'KEY_MUTE', t: 'Mute' }, { k: 'KEY_VOLDOWN', t: 'Vol −' }],
    [{ k: 'KEY_PLAY', t: 'Play' }, { k: 'KEY_PAUSE', t: 'Pause' }, { k: 'KEY_INFO', t: 'Info' }]
  ];

  var OPTSPEC = [
    { k: 'interval_seconds', t: 'int', nullable: true, label: 'Seconds per photo', hint: 'at least 2' },
    { k: 'fit', t: 'enum', nullable: true, opts: ['contain', 'cover'], label: 'How photos fit the screen' },
    { k: 'base_url', t: 'text', nullable: true, label: 'Server address for this TV', hint: 'only for a host with more than one network' },
    { k: 'browser_app_id', t: 'text', nullable: true, label: 'Browser app id', hint: 'only if probing picks the wrong one' },
    { k: 'open_with', t: 'enum', nullable: false, dflt: 'auto', opts: ['auto', 'api', 'macro', 'homepage'], label: 'How to open the browser' },
    { k: 'open_macro', t: 'keys', nullable: false, dflt: [], label: 'Open macro (keys)', hint: 'e.g. KEY_HOME,@1200,KEY_LEFT*3,KEY_ENTER' },
    { k: 'exit_macro', t: 'keys', nullable: false, dflt: [], label: 'Exit macro (keys)', hint: 'blank uses the shared exit macro' },
    { k: 'fullscreen_key', t: 'text', nullable: true, label: 'Fullscreen key', hint: 'untick inherit and leave it blank to send no key at all' },
    { k: 'wake_delay_seconds', t: 'int', nullable: false, dflt: 8, label: 'Wait after waking (s)' },
    { k: 'launch_wait_seconds', t: 'int', nullable: false, dflt: 30, label: 'Wait for the page (s)', hint: 'a cold browser plus big photos can take over 10s' },
    { k: 'power_off_mode', t: 'enum', nullable: false, dflt: 'auto', opts: ['auto', 'key', 'art'], label: 'Power off using' },
    { k: 'frame', t: 'tri', nullable: true, label: 'Frame TV', hint: 'inherit = detect from the TV' }
  ];

  function optId(k) { return 'opt-' + k; }
  function inhId(k) { return 'inh-' + k; }

  function parseKeys(s) {
    var raw = String(s || '').split(/[,+]/);
    var out = [];
    for (var i = 0; i < raw.length; i++) {
      var tok = raw[i].replace(/^\s+|\s+$/g, '');
      if (!tok) { continue; }
      if (tok.charAt(0) === '@') { out.push(tok); continue; }
      tok = tok.toUpperCase();
      if (tok.indexOf('KEY_') !== 0) { tok = 'KEY_' + tok; }
      out.push(tok);
    }
    return out;
  }

  function tvpRenderOptions() {
    var host = byId('options');
    if (!host) { return; }
    clear(host);
    var opts = TVP.tv.options || {};
    TVP.optsRendered = true;
    for (var i = 0; i < OPTSPEC.length; i++) {
      var s = OPTSPEC[i];
      var cur = opts[s.k];
      /* A non-nullable option that the server did not send falls back to the
         documented default (contract 3.4) rather than to blank - saving a blank
         would write a value outside the option's enum. */
      if (!s.nullable && (cur === null || cur === undefined) && s.dflt !== undefined) { cur = s.dflt; }
      var inherit = s.nullable && (cur === null || cur === undefined);
      var control;
      if (s.t === 'enum') {
        control = el('select', { 'class': 'input', id: optId(s.k) });
        for (var j = 0; j < s.opts.length; j++) {
          control.appendChild(el('option', { value: s.opts[j] }, s.opts[j]));
        }
        if (cur) { control.value = String(cur); }
      } else if (s.t === 'tri') {
        control = el('select', { 'class': 'input', id: optId(s.k) }, [
          el('option', { value: 'true' }, 'yes'),
          el('option', { value: 'false' }, 'no')
        ]);
        control.value = cur ? 'true' : 'false';
      } else if (s.t === 'keys') {
        control = el('input', {
          'class': 'input grow mono', id: optId(s.k),
          value: isArr(cur) ? cur.join(',') : (cur || ''),
          placeholder: 'KEY_HOME,@800,KEY_ENTER', autocapitalize: 'characters', spellcheck: 'false'
        });
      } else {
        control = el('input', {
          'class': 'input' + (s.t === 'int' ? ' num' : ' grow'), id: optId(s.k),
          value: (cur === null || cur === undefined) ? '' : String(cur),
          inputmode: s.t === 'int' ? 'numeric' : null,
          placeholder: inherit ? 'inherited' : ''
        });
      }
      if (inherit) { control.disabled = true; }
      var head = [el('span', {}, s.label)];
      if (s.nullable) {
        var cb = el('input', { type: 'checkbox', id: inhId(s.k), 'data-change': 'toggle-inherit', 'data-name': s.k });
        if (inherit) { cb.checked = true; }
        head.push(el('span', { 'class': 'spacer' }));
        head.push(el('label', { 'class': 'chk' }, [cb, 'inherit']));
      }
      host.appendChild(el('div', {}, [
        el('div', { 'class': 'row tight', style: 'font-size:13px;color:var(--dim)' }, head),
        control,
        s.hint ? el('div', { 'class': 'hint tight' }, s.hint) : null
      ]));
    }
  }

  function tvpCollectOptions() {
    var out = {};
    for (var i = 0; i < OPTSPEC.length; i++) {
      var s = OPTSPEC[i];
      var inh = byId(inhId(s.k));
      if (s.nullable && inh && inh.checked) { out[s.k] = null; continue; }
      var raw = val(optId(s.k));
      if (s.t === 'int') {
        if (raw === '') { out[s.k] = null; }
        else { out[s.k] = Math.round(num(raw, 0)); }
      } else if (s.t === 'tri') {
        out[s.k] = (raw === 'true');
      } else if (s.t === 'keys') {
        out[s.k] = parseKeys(raw);
      } else {
        out[s.k] = raw;
      }
    }
    return out;
  }

  function tvpRenderStatus() {
    var host = byId('tvstatus');
    if (!host) { return; }
    clear(host);
    var row = TVP.row || {};
    var tv = TVP.tv || {};
    var merged = {
      alias: tv.alias || ALIAS, ip: row.ip || tv.ip, model: row.model,
      power: row.power, frame: row.frame, browser: row.browser,
      heartbeat_age: row.heartbeat_age, playlist: row.playlist,
      paired: row.paired !== undefined ? row.paired : tv.paired,
      verified_how: row.verified_how || tv.verified_how,
      identify_number: row.identify_number,
      state: row.state, detail: row.detail,
      homepage_confirmed: tv.homepage_confirmed
    };
    host.appendChild(el('div', { 'class': 'row' }, [
      el('span', { 'class': 'strong' }, tv.label || merged.alias),
      stateBadge(merged)
    ]));
    host.appendChild(el('div', { 'class': 'meta' }, metaBits(merged)));
    if (row.detail) { host.appendChild(el('div', { 'class': 'hint tight' }, String(row.detail))); }
    if (row.busy) {
      host.appendChild(el('div', { 'class': 'busyline' },
        (row.detail || 'working') + (row.busy_left ? ' — ' + Math.round(num(row.busy_left, 0)) + 's' : '')));
    }
    if (merged.paired === false) {
      host.appendChild(el('div', { 'class': 'note bad' },
        'This TV is not paired, so nothing can be sent to it. Press Pair below and then choose ALLOW on the TV screen. Pairing only works from a host on the same subnet as the TV.'));
    }
  }

  function tvpRenderPlaylists() {
    var sel = byId('tv-playlist');
    if (!sel) { return; }
    api('GET', '/api/playlists', null, function (err, data) {
      if (err) { return; }
      var list = (data && data.playlists) || [];
      var want = sel.value || TVP.row.playlist || (data && data.shared) || '';
      clear(sel);
      for (var i = 0; i < list.length; i++) {
        sel.appendChild(el('option', { value: list[i].name },
          list[i].name + ' (' + plural(num(list[i].count, 0), 'image', 'images') + ')'));
      }
      if (!list.length) { sel.appendChild(el('option', { value: '' }, 'no playlists yet')); }
      if (want) { sel.value = want; }
    });
  }

  function tvpRenderRecorder() {
    var host = byId('reclist');
    if (!host) { return; }
    clear(host);
    if (!TVP.seq.length) {
      host.appendChild(el('li', { 'class': 'wait' }, 'nothing recorded yet'));
    }
    for (var i = 0; i < TVP.seq.length; i++) {
      var tok = TVP.seq[i];
      host.appendChild(el('li', { 'class': tok.charAt(0) === '@' ? 'wait' : '' }, tok));
    }
    setText(byId('recseq'), TVP.seq.join(','));
    var b = byId('rec-toggle');
    if (b) {
      b.className = 'btn' + (TVP.rec ? ' on' : '');
      setText(b, TVP.rec ? 'Recording - stop' : 'Record');
    }
    var pad = byId('pad');
    if (pad) { pad.className = 'pad' + (TVP.rec ? ' recording' : ''); }
  }

  function tvpRefresh() {
    api('GET', '/api/tvs/' + encodeURIComponent(ALIAS), null, function (err, data) {
      if (err) { setText(byId('tvstatus'), 'cannot read this TV: ' + err); return; }
      var tv = (data && data.tv && typeof data.tv === 'object') ? data.tv : (data || {});
      var row = {};
      if (data && data.status && typeof data.status === 'object') { row = data.status; }
      else if (data && data.row && typeof data.row === 'object') { row = data.row; }
      else if (data && data.state) { row = data; }
      TVP.tv = tv;
      TVP.row = row;
      tvpRenderStatus();
      /* Render the options form once, on the first reply that actually carries
         options - the placeholder form drawn at load has nothing in it, and
         re-rendering on every poll would wipe out half-typed edits. */
      if (!TVP.optsRendered) { tvpRenderOptions(); }
      if (!TVP.seq.length && tv.options && isArr(tv.options.open_macro) && tv.options.open_macro.length) {
        TVP.seq = tv.options.open_macro.slice(0);
        tvpRenderRecorder();
      }
      var box = byId('hp-confirmed');
      if (box) { box.checked = !!tv.homepage_confirmed; }
    });
    api('GET', '/api/homepages', null, function (err, hp) {
      if (err || !hp) { return; }
      var url = (hp.per_tv && hp.per_tv[ALIAS]) || hp.homepage_url || '';
      TVP.homepage = url;
      setText(byId('tv-homepage'), url || 'set the server address in setup first');
      var c = byId('hp-copy');
      if (c) { c.setAttribute('data-arg', url); }
    });
  }

  function sendKey(key) {
    api('POST', '/api/tvs/' + encodeURIComponent(ALIAS) + '/key/' + encodeURIComponent(key), {},
      function (err, data) {
        if (err) { toast(key + ': ' + err, 'error', 9000); return; }
        var msg = (data && data.message) || '';
        if (data && data.ok === false) { toast(key + ': ' + (msg || 'refused'), 'error', 12000); }
        else if (msg && !isOk(msg)) { toast(msg, levelOfText(msg), 12000); }
      });
    if (TVP.rec) {
      TVP.seq.push(key);
      tvpRenderRecorder();
    }
  }

  function initRemote() {
    /* Remote pad */
    var pad = byId('pad');
    if (pad) {
      clear(pad);
      for (var r = 0; r < PAD.length; r++) {
        for (var c = 0; c < PAD[r].length; c++) {
          var cell = PAD[r][c];
          if (!cell.k) { pad.appendChild(el('span', {})); continue; }
          pad.appendChild(btn(cell.t, 'key', { key: cell.k }));
        }
      }
    }

    ACTIONS['key'] = function (n, d) { sendKey(d.key); };
    ACTIONS['tv-verb'] = function (n, d) { tvAction(ALIAS, d.arg, null, { done: function () { tvpRefresh(); } }); };
    ACTIONS['tv-show'] = function () {
      var p = val('tv-playlist');
      tvAction(ALIAS, 'show', p || null, { done: function () { tvpRefresh(); } });
    };
    ACTIONS['send-keys'] = function () {
      var seq = parseKeys(val('keyseq')).join(',');
      if (!seq) { toast('type a key sequence first, e.g. KEY_HOME,@800,KEY_ENTER', 'warn'); return; }
      tvAction(ALIAS, 'keys', seq, {});
    };
    ACTIONS['set-volume'] = function () {
      var v = val('volume');
      if (v === '') { toast('type a volume from 0 to 100', 'warn'); return; }
      tvAction(ALIAS, 'volume', v, {});
    };
    ACTIONS['mute'] = function (n, d) { tvAction(ALIAS, 'mute', d.arg, {}); };
    ACTIONS['launch-app'] = function () {
      var id = val('appid');
      if (!id) { toast('type an app id', 'warn'); return; }
      tvAction(ALIAS, 'app', id, {});
    };
    ACTIONS['run-macro'] = function () {
      var name = val('macroname');
      if (!name) { toast('type the macro name from config.json', 'warn'); return; }
      tvAction(ALIAS, 'macro', name, {});
    };

    /* Macro recorder */
    ACTIONS['rec-toggle'] = function () { TVP.rec = !TVP.rec; tvpRenderRecorder(); };
    ACTIONS['rec-wait'] = function () {
      var ms = Math.round(num(val('waitms'), 500));
      if (ms < 1) { ms = 1; }
      TVP.seq.push('@' + ms);
      tvpRenderRecorder();
    };
    ACTIONS['rec-undo'] = function () { TVP.seq.pop(); tvpRenderRecorder(); };
    ACTIONS['rec-clear'] = function () { TVP.seq = []; tvpRenderRecorder(); };
    ACTIONS['rec-load'] = function () {
      var o = TVP.tv.options || {};
      TVP.seq = isArr(o.open_macro) ? o.open_macro.slice(0) : [];
      tvpRenderRecorder();
    };
    ACTIONS['rec-replay'] = function () {
      if (!TVP.seq.length) { toast('record something first', 'warn'); return; }
      /* Replay through the keys verb, not one press at a time: the server keeps
         the inter-key gap Tizen needs and re-checks the control port. */
      tvAction(ALIAS, 'keys', TVP.seq.join(','), {});
    };
    ACTIONS['rec-save'] = function (n, d) {
      var which = d.arg === 'exit' ? 'exit_macro' : 'open_macro';
      var body = { options: {} };
      body.options[which] = TVP.seq.slice(0);
      api('PATCH', '/api/tvs/' + encodeURIComponent(ALIAS), body, function (err, data) {
        if (err) { toast('could not save the macro: ' + err, 'error', 12000); return; }
        toast((data && data.message) || ('saved as ' + which), 'ok');
        tvpRefresh();
      });
    };

    /* Options */
    CHANGES['toggle-inherit'] = function (n, d) {
      var c = byId(optId(d.name));
      if (c) { c.disabled = !!n.checked; }
    };
    ACTIONS['save-options'] = function () {
      api('PATCH', '/api/tvs/' + encodeURIComponent(ALIAS), { options: tvpCollectOptions() },
        function (err, data) {
          if (err) { toast('could not save: ' + err, 'error', 12000); return; }
          toast((data && data.message) || 'options saved', 'ok');
          api('GET', '/api/tvs/' + encodeURIComponent(ALIAS), null, function (e2, d2) {
            if (e2) { return; }
            TVP.tv = (d2 && d2.tv && typeof d2.tv === 'object') ? d2.tv : (d2 || {});
            tvpRenderOptions();
          });
        });
    };
    ACTIONS['reset-options'] = function () { tvpRenderOptions(); };

    /* Pairing */
    ACTIONS['pair'] = function () {
      runJob('POST', '/api/tvs/' + encodeURIComponent(ALIAS) + '/pair', { wait: 90 },
        'pairing ' + ALIAS, { log: byId('pairlog'), done: function () { tvpRefresh(); } });
    };
    ACTIONS['verify'] = function () {
      runJob('POST', '/api/tvs/' + encodeURIComponent(ALIAS) + '/verify', {},
        'verifying ' + ALIAS, { log: byId('pairlog'), done: function () { tvpRefresh(); } });
    };
    ACTIONS['unpair'] = function () {
      if (!window.confirm('Forget the pairing token for ' + ALIAS + '?')) { return; }
      api('POST', '/api/tvs/' + encodeURIComponent(ALIAS) + '/unpair', {}, function (err, data) {
        if (err) { toast('could not unpair: ' + err, 'error', 12000); return; }
        toast((data && data.message) || 'token forgotten', 'ok');
        tvpRefresh();
      });
    };

    /* Identity */
    ACTIONS['set-ip'] = function () {
      var ip = val('newip');
      if (!ip) { toast('type the new address', 'warn'); return; }
      var body = { ip: ip };
      var mac = val('newmac');
      if (mac) { body.mac = mac; }
      api('PATCH', '/api/tvs/' + encodeURIComponent(ALIAS), body, function (err, data) {
        if (err) { toast('could not set the address: ' + err, 'error', 12000); return; }
        toast((data && data.message) || ('address set to ' + ip), 'ok', 10000);
        tvpRefresh();
      });
    };
    ACTIONS['set-label'] = function () {
      var label = val('newlabel');
      if (!label) { toast('type a label', 'warn'); return; }
      api('PATCH', '/api/tvs/' + encodeURIComponent(ALIAS), { label: label }, function (err, data) {
        if (err) { toast('could not save: ' + err, 'error', 12000); return; }
        toast((data && data.message) || 'label saved', 'ok');
        tvpRefresh();
      });
    };
    ACTIONS['rename'] = function () {
      var alias = val('newalias');
      if (!alias) { toast('type the new short name', 'warn'); return; }
      if (!window.confirm('Rename ' + ALIAS + ' to ' + alias + '? Any controller URL using the old name stops working.')) { return; }
      api('PATCH', '/api/tvs/' + encodeURIComponent(ALIAS), { alias: alias }, function (err, data) {
        if (err) { toast('could not rename: ' + err, 'error', 12000); return; }
        toast((data && data.message) || 'renamed', 'ok');
        location.href = '/ui/tv/' + encodeURIComponent(alias);
      });
    };
    ACTIONS['delete-tv'] = function () {
      if (!window.confirm('Remove ' + ALIAS + ' from TVHub? Photos and playlists are not touched.')) { return; }
      api('DELETE', '/api/tvs/' + encodeURIComponent(ALIAS), null, function (err) {
        if (err) { toast('could not remove it: ' + err, 'error', 12000); return; }
        location.href = '/ui/';
      });
    };
    CHANGES['hp-confirm'] = function (n) {
      api('POST', '/api/tvs/' + encodeURIComponent(ALIAS) + '/homepage', { confirmed: !!n.checked },
        function (err) {
          if (err) { toast('could not record that: ' + err, 'error', 12000); n.checked = !n.checked; }
        });
    };

    setText(byId('tvalias'), ALIAS);
    var back = byId('backlink');
    if (back) { back.setAttribute('href', '/ui/'); }
    setText(byId('options'), 'loading…');
    tvpRenderRecorder();
    tvpRenderPlaylists();
    var p = poller(tvpRefresh, 5000);
    p.start();
  }

  /* =====================================================================
   *  boot
   * ===================================================================== */

  function start() {
    bindDelegation();
    toastBox();
    if (PAGE === 'dashboard') { initDashboard(); }
    else if (PAGE === 'setup') { initSetup(); }
    else if (PAGE === 'photos') { initPhotos(); }
    else if (PAGE === 'remote') { initRemote(); }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, false);
  } else {
    start();
  }
}());
