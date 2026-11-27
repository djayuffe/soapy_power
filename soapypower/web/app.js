'use strict';

const $ = id => document.getElementById(id);
let token = document.querySelector('meta[name="csrf-token"]').content;
const model = {
  state: null, selected: null, detail: null, epoch: 0,
  connected: false, lastState: 0, lastDetail: 0, polling: false,
  timer: null, failures: 0, action: null, awaiting: null, awaitingJob: null, confirmation: null,
  detailController: null, toastTimer: null, drawPending: false, tableSignature: null, pendingLocal: null, chartSignature: null
};
const finite = value => typeof value === 'number' && Number.isFinite(value);
const format = (value, digits = 1) => finite(value)
  ? value.toLocaleString(undefined, {maximumFractionDigits: digits}) : '—';
const duration = value => finite(value) && value >= 0
  ? `${Math.floor(value / 60)}m ${Math.floor(value % 60)}s` : '—';
const stamp = value => value && Number.isFinite(Date.parse(value))
  ? new Date(value).toLocaleString() : '—';
const put = (id, value) => { $(id).textContent = value ?? '—'; };
const active = session => ['running', 'paused'].includes(session?.status);
const fresh = () => model.connected && Date.now() - model.lastState < 10000;

function notify(message) {
  put('toast', message);
  $('toast').hidden = false;
  clearTimeout(model.toastTimer);
  model.toastTimer = setTimeout(() => { $('toast').hidden = true; }, 7000);
}

async function request(path, data, refreshToken = true, signal = null, requestId = null) {
  if (data && !requestId) requestId = crypto.randomUUID().replaceAll('-', '');
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener('abort', abort, {once: true});
  const timer = setTimeout(abort, data ? 15000 : 7000);
  try {
    const response = await fetch(path, {
      cache: 'no-store', signal: controller.signal,
      ...(data ? {method: 'POST', headers: {
        'Content-Type': 'application/json', 'X-CSRF-Token': token, 'X-Request-ID': requestId
      }, body: JSON.stringify(data)} : {})
    });
    if (response.status === 403 && data && refreshToken) {
      const page = await fetch('/', {cache: 'no-store', signal: controller.signal});
      const html = new DOMParser().parseFromString(await page.text(), 'text/html');
      const next = html.querySelector('meta[name="csrf-token"]')?.content;
      if (next && next !== token) {
        token = next;
        return request(path, data, false, signal, requestId);
      }
    }
    const text = await response.text();
    let result;
    try { result = JSON.parse(text); }
    catch { throw new Error(`Invalid server response (HTTP ${response.status}).`); }
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  } catch (error) {
    if (error.name === 'AbortError') {
      if (signal?.aborted) throw error;
      throw new Error(data
        ? 'Request timed out. Its outcome is unknown; refresh job status before retrying.'
        : 'Server response timed out.');
    }
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}

function selectSession(id) {
  model.detailController?.abort();
  model.selected = id;
  if(typeof satelliteSelectionChanged==='function')satelliteSelectionChanged();
  model.epoch += 1;
  model.detail = null;
  model.lastDetail = 0;
  renderEmpty();
  renderActions();
}

async function poll() {
  if (model.polling) return;
  clearTimeout(model.timer);
  model.polling = true;
  try {
    const state = await request('/api/state');
    if (!Array.isArray(state.sessions) || !Array.isArray(state.jobs)) {
      throw new Error('Server returned an invalid workspace snapshot.');
    }
    model.state = state;
    model.connected = true;
    model.lastState = Date.now();
    model.failures = 0;
    put('connection', 'Connected');
    put('runner-label', state.runner_ready ? 'Runner available' : 'Runner unavailable');
    if (model.awaiting) {
      const created = state.sessions.find(s => !model.awaiting.has(s.id) && /-\d{8}-\d{6}$/.test(s.id));
      if (created) { selectSession(created.id); model.awaiting = null; model.awaitingJob=null; }
      else { const job=state.jobs.find(item=>item.id===model.awaitingJob); if(job&&['finished','rejected','interrupted'].includes(job.state)){model.awaiting=null;model.awaitingJob=null;notify('Job ended without a new capture. Inspect the runner status and log.');} }
    }
    if (!model.selected && state.sessions.length) {
      selectSession((state.sessions.find(active) || state.sessions[0]).id);
    }
    renderSessions();
    renderJobs();
    renderActions();
    if (model.selected) {
      const id = model.selected, epoch = model.epoch;
      const controller = new AbortController();
      model.detailController = controller;
      try {
        const detail = await request('/api/session?id=' + encodeURIComponent(id), null, true, controller.signal);
        if (epoch === model.epoch && id === model.selected) {
          if (detail.session?.id !== id) throw new Error('Session response did not match the selection.');
          model.detail = detail;
          model.lastDetail = Date.now();
          renderDetail();
        }
      } catch (error) {
        if (error.name !== 'AbortError') {
          model.lastDetail = 0;
          showNotice('Session could not be refreshed: ' + error.message);
        }
      }
    }
  } catch (error) {
    model.connected = false;
    model.failures += 1;
    put('connection', 'Disconnected');
    showNotice('Connection lost. Displayed values are saved snapshots. ' + error.message);
  } finally {
    model.polling = false;
    renderActions();
    const delay = document.hidden ? 15000 : Math.min(20000, 2000 * 2 ** Math.min(model.failures, 3));
    model.timer = setTimeout(poll, delay);
  }
}

function refresh() {
  clearTimeout(model.timer);
  if (!model.polling) poll();
}

function showNotice(message) {
  put('notice', message);
  $('notice').hidden = !message;
}

function renderActions() {
  const online = fresh();
  const state = model.state;
  const selected = model.detail?.session?.id === model.selected;
  const detailFresh = selected && Date.now() - model.lastDetail < 10000;
  const canQueue = online && !!state?.runner_ready && !state?.busy && !model.action && !model.awaiting;
  for (const id of ['new-scan', 'scan-nav', 'probe-button', 'start-scan']) $(id).disabled = !canQueue;
  const detail = model.detail;
  $('generate-report').disabled = !canQueue || !detailFresh || active(detail?.session)
    || !/^(superscan|hopscan)-\d{8}-\d{6}$/.test(model.selected || '');
  const pending = model.pendingLocal?.session === model.selected || detail?.pending_control || (active(detail?.session) && detail?.stop_requested);
  for (const [id, available] of [['pause-scan', detail?.can_pause], ['resume-scan', detail?.can_resume],
    ['retry-scan', detail?.can_retry], ['stop-scan', detail?.can_stop]]) {
    $(id).hidden = !available;
    $(id).disabled = !online || !detailFresh || !!model.action ||
      (id !== 'stop-scan' && !!pending) || (id === 'stop-scan' && detail?.stop_requested) ||
      (id === 'retry-scan' && !active(detail?.session) && !canQueue);
  }
  $('confirm-stop').disabled = !!model.action || !online || !detailFresh;
  if (online) {
    if (!state?.runner_ready) showNotice('Saved-results mode. ' + (state.runner_message || 'The scan runner is not loaded.'));
    else if (state.busy) showNotice('Runner reserved by a queued or active job.');
    else if (detailFresh) showNotice(null);
  }
}

function renderSessions() {
  const body = $('session-list');
  const signature=JSON.stringify([model.selected,model.state.sessions]);
  if(signature===model.tableSignature)return;
  model.tableSignature=signature;
  const focused=body.contains(document.activeElement)?document.activeElement.dataset.session:null;
  const fragment = document.createDocumentFragment();
  const known = new Set(['running', 'paused', 'completed', 'cancelled', 'incomplete', 'failed',
    'interrupted', 'saved', 'reporting', 'report_failed', 'invalid']);
  for (const session of model.state.sessions) {
    const row = document.createElement('tr');
    row.classList.toggle('selected', session.id === model.selected);
    const cell = () => row.appendChild(document.createElement('td'));
    const button = document.createElement('button');
    button.textContent = session.id; button.dataset.session=session.id;
    button.onclick = () => {
      model.awaiting = null;
      selectSession(session.id);
      renderSessions();
      refresh();
    };
    cell().append(button);
    const badge = document.createElement('span');
    badge.className = 'status ' + (known.has(session.status) ? session.status : 'invalid');
    badge.textContent = String(session.status || 'unknown').replaceAll('_', ' ');
    cell().append(badge);
    cell().textContent = duration(session.credited_s);
    cell().textContent = format(session.records, 0);
    const reportCell = cell();
    if (session.report_url?.startsWith('/files/')) {
      const link = document.createElement('a');
      link.href = session.report_url; link.textContent = 'Report'; link.target = '_blank'; link.rel = 'noopener';
      reportCell.append(link);
    } else reportCell.textContent = '—';
    fragment.append(row);
  }
  if (!fragment.childNodes.length) {
    const row = document.createElement('tr'), cell = document.createElement('td');
    cell.colSpan = 5; cell.className = 'empty-row'; cell.textContent = 'No saved sessions.';
    row.append(cell); fragment.append(row);
  }
  body.replaceChildren(fragment);
  if(focused) [...body.querySelectorAll('button')].find(button=>button.dataset.session===focused)?.focus({preventScroll:true});
}

function renderJobs() {
  const job = model.state?.jobs?.[0];
  put('job-status', job ? `${job.id || job.mode || 'Job'} · ${job.state || 'unknown'}${job.exit != null ? ' · exit ' + job.exit : ''}` : 'No runner jobs recorded');
}

function renderEmpty() {
  for (const id of ['acquisition-state', 'duration-value', 'record-value', 'recovery-value', 'temperature',
    'peak-value', 'p999-value', 'lo-value', 'continuity-value', 'rf-value', 'rate-value', 'gain-value',
    'resolution-value', 'frame-time', 'progress-percent']) put(id, '—');
  put('selected-name', model.selected || 'No session selected');
  put('acquisition-note', model.selected ? 'Loading selected session' : 'Select a session');
  put('spectrum-subtitle', 'No frame loaded');
  $('spectrum-empty').hidden = false;
  $('report-link').hidden = true;
  $('open-expert-job').hidden = true;
  $('control-status').hidden = true;
  $('progress-bar').style.width = '0%';
  put('log', 'No log loaded.');
  model.chartSignature=null;
  scheduleDraw();
}

function renderDetail() {
  const detail = model.detail, session = detail.session, live = detail.live;
  const local=model.pendingLocal;
  if(local?.session===session.id && (!active(session)||detail.pending_control||(detail.controls||[]).some(event=>event.id===local.id)||detail.stop_requested)) model.pendingLocal=null;
  put('acquisition-state', String(session.status).replaceAll('_', ' '));
  put('acquisition-note', session.requested_s != null ? `${session.attempts} finalized attempt${session.attempts === 1 ? '' : 's'}` : 'No supervision manifest');
  put('duration-value', duration(session.credited_s));
  $('open-expert-job').hidden = !session.expert;
  put('duration-note', session.expert ? 'Expert process budget; no qualified-time credit' : session.status === 'running' && finite(session.live_estimated_s)
    ? `Unvalidated span estimate: ${duration(session.live_estimated_s)}` : 'Confirmed accepted time');
  put('record-value', format(session.records, 0));
  put('record-note', session.expert ? 'Rows passing all recorded receive guards' : session.records == null ? 'Awaiting confirmed totals' : 'Confirmed saved records');
  put('recovery-value', session.recoveries);
  put('selected-name', session.id);
  const report = $('report-link');
  report.hidden = !session.report_url?.startsWith('/files/');
  if (!report.hidden) report.href = session.report_url;
  const percent = finite(session.requested_s) && session.requested_s > 0 && finite(session.credited_s)
    ? Math.min(100, 100 * session.credited_s / session.requested_s) : null;
  $('progress-bar').style.width = `${percent ?? 0}%`;
  put('progress-percent', percent == null ? '—' : format(percent) + '%');
  put('progress-description', session.expert ? `Expert process budget: ${duration(session.requested_s)} · controls in Workbench` : session.requested_s
    ? `${duration(session.credited_s)} accepted / ${duration(session.requested_s)} requested` : 'No supervised time budget');
  const pending = active(session) && detail.stop_requested ? 'Abort requested. Preserving accepted data.'
    : detail.pending_control ? `${detail.pending_control} requested; waiting for supervisor acknowledgement.`
    : session.status === 'paused' ? 'Paused. RX stream closed; the runner remains reserved.'
    : session.status === 'interrupted' ? 'Supervisor is not running. Live controls are unavailable; inspect the retained log.' : '';
  put('control-status', pending); $('control-status').hidden = !pending;
  put('retry-scan', active(session) ? 'Retry attempt' : 'Repeat scan');
  put('stop-scan', detail.stop_requested ? 'Aborting…' : 'Abort');
  const list = $('control-history'); list.replaceChildren();
  for (const event of detail.controls || []) {
    const item = document.createElement('li');
    item.textContent = `${event.action} · ${stamp(event.acknowledged_utc)} · ${duration(event.credited_seconds)} accepted`;
    list.append(item);
  }
  if (!list.children.length) list.textContent = 'No operator controls recorded.';
  put('log', (detail.report_error ? `REPORT ERROR\n${detail.report_error}\n\n` : '') + (detail.log || 'No acquisition log.'));
  $('spectrum-empty').hidden = !!live;
  put('frame-status', !live ? 'NO FRAME' : live.qualified ? 'QUALIFIED RECORD' : 'UNQUALIFIED PREVIEW');
  put('spectrum-subtitle', live
    ? `LO ${format(finite(live.center_hz) ? live.center_hz / 1e6 : null, 4)} MHz  |  ${format(finite(live.rate_hz) ? live.rate_hz / 1e6 : null, 2)} MS/s  |  ${format(live.bins, 0)} bins`
    : 'No complete spectrum record available');
  put('frame-time', live ? stamp(live.stop) : '—');
  put('power-unit', live ? live.unit || 'Power unit unavailable' : 'Power spectrum · uncalibrated');
  put('quality-badge','RECORDED STATE');
  put('temperature', finite(live?.temperature_c) ? `${format(live.temperature_c)} °C` : '—');
  put('lo-value', live?.lo_locked === true ? 'Locked' : live?.lo_locked === false ? 'Unlocked' : 'Unknown');
  put('rf-value', live?.rf_valid === true ? 'Accepted' : live?.rf_valid === false ? 'Rejected' : 'Unknown');
  put('continuity-value', typeof live?.continuity === 'string' ? live.continuity : 'Unknown');
  put('rate-value', finite(live?.rate_hz) ? `${format(finite(live.rate_hz) ? live.rate_hz / 1e6 : null, 2)} MS/s` : '—');
  put('gain-value', live?.gain ? Object.entries(live.gain).map(([k,v]) => `${k} ${format(v)} dB`).join(', ') : '—');
  put('resolution-value', live?.rate_hz && live.bins ? `${format(live.rate_hz / live.bins, 2)} Hz` : '—');
  put('receiver-name', typeof live?.hardware === 'string' ? live.hardware : 'Receiver');
  for (const [key, id, limitKey] of [['max_component', 'peak', 'maximum_limit'], ['p999_component', 'p999', 'p999_limit']]) {
    const value = live?.headroom?.[key], reference = live?.headroom?.[limitKey];
    put(id+'-limit',finite(reference)?`Saved guard limit: ${format(reference,4)}`:'Saved guard limit unavailable');
    put(id + '-value', format(value, 4));
    $(id + '-meter').style.width = `${finite(value) ? Math.min(100, Math.max(0, value * 100)) : 0}%`;
    $(id + '-meter').classList.toggle('exceeded', finite(value) && finite(reference) && value >= reference);
  }
  const chartSignature=JSON.stringify([session.id,live?.stop,live?.bins,live?.rate_hz,live?.gain,live?.history?.length]);
  if(chartSignature!==model.chartSignature){model.chartSignature=chartSignature;scheduleDraw();}
  renderActions();
}

function chartContext(id) {
  const canvas = $(id), bounds = canvas.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
  if (!bounds.width || !bounds.height) return null;
  canvas.width = Math.round(bounds.width * ratio); canvas.height = Math.round(bounds.height * ratio);
  const context = canvas.getContext('2d'); context.scale(ratio, ratio);
  return {context, width: bounds.width, height: bounds.height};
}
function scheduleDraw() {
  if (model.drawPending) return;
  model.drawPending = true;
  requestAnimationFrame(() => { model.drawPending = false; drawCharts(); });
}
function drawCharts() {
  const plot = chartContext('spectrum'), waterfall = chartContext('waterfall');
  if (!plot || !waterfall) return;
  const live = model.detail?.live;
  const {context:c, width:w, height:h} = plot;
  c.fillStyle = '#121a23'; c.fillRect(0, 0, w, h);
  const values = live?.power?.filter(finite) || [];
  let low = values.length ? Math.floor(Math.min(...values) / 10) * 10 - 5 : -120;
  let high = values.length ? Math.ceil(Math.max(...values) / 10) * 10 + 5 : -60;
  if (high - low < 20) high = low + 20;
  const left = 47, right = Math.max(left + 1, w - 17), top = 14, bottom = h - 30;
  c.font = '10px ui-monospace, monospace'; c.strokeStyle = '#2a3747'; c.fillStyle = '#a4b0bf'; c.lineWidth = .5;
  for (let i = 0; i <= 4; i++) {
    const y = top + (bottom - top) * i / 4;
    c.beginPath(); c.moveTo(left, y); c.lineTo(right, y); c.stroke();
    c.textAlign='left';
    c.fillText(format(high - (high - low) * i / 4, 0), 8, y + 3);
    const x = left + (right - left) * i / 4;
    const frequencies = live?.frequencies_hz;
    const firstHz=frequencies?.[0], lastHz=frequencies?.at(-1);
    const hz=finite(firstHz)&&finite(lastHz)?firstHz+(lastHz-firstHz)*i/4:null;
    c.textAlign=i===0?'left':i===4?'right':'center';
    c.fillText(finite(hz) ? format(hz / 1e6, 3) : '—', x, h - 9);
  }
  if (live && values.length) {
    c.beginPath(); let pen = false;
    live.power.forEach((value, index) => {
      if (!finite(value)) { pen = false; return; }
      const frequencies=live.frequencies_hz, first=frequencies?.[0], last=frequencies?.at(-1), hz=frequencies?.[index];
      const offset=finite(first)&&finite(last)&&finite(hz)&&last!==first?(hz-first)/(last-first):index/Math.max(1,live.power.length-1);
      const x=left+offset*(right-left);
      const y = bottom - (value - low) / (high - low) * (bottom - top);
      if (pen) c.lineTo(x, y); else c.moveTo(x, y);
      pen = true;
    });
    c.strokeStyle = '#d8b76a'; c.lineWidth = 1.2; c.stroke();
  }
  if(typeof drawSatelliteOverlay==='function')drawSatelliteOverlay(c,{left,right,top,bottom},live);
  if(typeof renderSatelliteFrame==='function')renderSatelliteFrame();
  const {context:a, width:ww, height:hh} = waterfall;
  a.fillStyle = '#121a23'; a.fillRect(0, 0, ww, hh);
  const rows = (live?.history || []).filter(row => finite(Date.parse(row.start)) && finite(Date.parse(row.stop)));
  if (!rows.length) { put('if-label', 'IF offset · no sampled visits'); return; }
  const end = Date.parse(rows.at(-1).stop), first = Date.parse(rows[0].start), span = Math.max(1, end - first);
  for (const row of rows) {
    const start = Date.parse(row.start), stop = Date.parse(row.stop);
    const y = (start - first) / span * hh, height = (stop - start) / span * hh;
    if (height <= 0) continue;
    // Sub-pixel visits may vanish at this scale; do not fill acquisition gaps.
    row.power.forEach((value, index) => {
      if (!finite(value)) return;
      const t = Math.max(0, Math.min(1, (value - low) / (high - low)));
      a.fillStyle = `hsl(${225 - 185 * t} 48% ${12 + 54 * t}%)`;
      a.fillRect(index / row.power.length * ww, y, ww / row.power.length + .25, height);
    });
  }
  if(typeof drawSatelliteWaterfall==='function')drawSatelliteWaterfall(a,live,rows,ww,hh,first,span);
  put('if-label', `IF ±${format(finite(live?.rate_hz)?live.rate_hz/2e6:null, 2)} MHz · ${rows.length} sampled visits · RF center varies`);
}

function configure() {
  const blade = $('device').value === 'bladerf';
  if (blade) $('workflow').value = 'fullrange';
  for (const option of $('workflow').options) option.disabled = blade && option.value !== 'fullrange';
  const workflow = $('workflow').value, satellite = !blade && workflow === 'satellite';
  document.querySelectorAll('.satellite-field').forEach(field => { field.hidden = !satellite; });
  $('blade-job').hidden = !blade; $('antenna-field').hidden = blade;
  $('gain').closest('label').hidden = blade;
  $('gain').querySelector('[value="auto"]').disabled = !satellite;
  if (!satellite && $('gain').value === 'auto') $('gain').value = '60';
  const summaries = {
    satellite: 'Supervised satellite bands. Pause/resume, bounded recovery and automatic reports. Time is credited only after record validation.',
    hopscan: '140–150 MHz hop scan with automatic analysis. Live pause/retry is unavailable for this workflow.',
    fullrange: '70–6000 MHz survey. Dither ON and OFF each use the selected duration; total acquisition is approximately twice that duration. Live pause/retry is unavailable.',
    baseline: 'Fixed baseline procedure uses its own capture lengths. The duration selector does not change those captures.',
    suite: '100–300 MHz experiment suite. Duration applies to each experiment, not to the entire suite.'
  };
  put('scan-summary', blade ? 'Uses the bladeRF + XB-200 gain/filter profile. Duration applies to each selected procedure. Live pause/retry is unavailable.' : summaries[workflow]);
  document.querySelector('.included').hidden = !satellite;
}
function openScan() { configure(); $('form-error').hidden = true; $('scan-dialog').showModal(); }
function confirmAction(action) {
  if (!model.detail || model.detail.session.id !== model.selected) return;
  const isActive = active(model.detail.session), cfg = model.detail.retry_config;
  model.confirmation = {action, session: model.selected, active: isActive};
  put('confirm-title', action === 'abort' ? 'Abort acquisition?' : isActive ? 'Retry current attempt?' : 'Repeat scan?');
  put('confirm-message', action === 'abort'
    ? 'Close RX, preserve qualified records and generate the available partial report. The requested duration may remain incomplete.'
    : isActive ? 'Preserve the accepted prefix, reopen RX and continue with the remaining time and current gain. A new measurement partition will be created.'
    : `Create a NEW session: ${cfg?.device}, ${cfg?.workflow}, ${duration(cfg?.duration_s)} per configured capture, ${cfg?.input}, gain ${cfg?.gain}. Check the physical input before proceeding.`);
  put('confirm-stop', action === 'abort' ? 'Abort and preserve data' : isActive ? 'Retry attempt' : 'Queue new session');
  $('confirm-dialog').showModal(); renderActions();
}
async function perform(action, data, onSuccess = null) {
  if (model.action) return;
  model.action = action; renderActions();
  try {
    const result = await request('/api/' + action, data);
    if (result.job_id && action !== 'report' && action !== 'probe') {
      model.awaiting = new Set(model.state?.sessions.map(session => session.id) || []);
      model.awaitingJob=result.job_id;
    }
    if(result.request_id || result.state==='stop_requested')model.pendingLocal={session:data.session,action,id:result.request_id};
    onSuccess?.(result);
    notify(result.job_id ? `Queued ${result.job_id}` : `${action} requested. Awaiting supervisor acknowledgement.`);
  } catch (error) { notify(error.message); throw error; }
  finally { model.action = null; renderActions(); refresh(); }
}

$('new-scan').onclick = openScan; $('scan-nav').onclick = openScan;
$('close-dialog').onclick = () => $('scan-dialog').close();
$('device').onchange = configure; $('workflow').onchange = configure;
$('refresh').onclick = refresh;
$('history-nav').onclick = () => document.querySelector('.session-panel').scrollIntoView({behavior: 'smooth'});
$('overview-nav').onclick = () => window.scrollTo({top: 0, behavior: 'smooth'});
$('probe-button').onclick = () => perform('probe', {device: $('device').value}).catch(() => {});
$('generate-report').onclick = () => perform('report', {session: model.selected}).catch(() => {});
$('pause-scan').onclick = () => perform('pause', {session: model.selected}).catch(() => {});
$('resume-scan').onclick = () => perform('resume', {session: model.selected}).catch(() => {});
$('stop-scan').onclick = () => confirmAction('abort');
$('retry-scan').onclick = () => confirmAction('retry');
$('cancel-stop').onclick = () => $('confirm-dialog').close();
$('confirm-stop').onclick = () => {
  const confirmation = model.confirmation;
  if(confirmation && (confirmation.session!==model.selected || (confirmation.action==='retry' && confirmation.active!==active(model.detail?.session)))) { $('confirm-dialog').close(); notify('Session state changed. Review the action again.'); return; }
  if (confirmation) perform(confirmation.action, {session: confirmation.session, ...(confirmation.action === 'retry' ? {mode: confirmation.active ? 'attempt' : 'repeat'} : {})}, () => $('confirm-dialog').close()).catch(() => {});
};
$('scan-form').onsubmit = async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  for (const key of ['duration_s', 'rate_msps', 'dither_khz']) data[key] = Number(data[key]);
  if (data.gain !== 'auto') data.gain = Number(data.gain);
  try { await perform('scan', data, () => $('scan-dialog').close()); }
  catch (error) { put('form-error', error.message); $('form-error').hidden = false; }
};
new ResizeObserver(scheduleDraw).observe($('spectrum'));
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
window.addEventListener('online', refresh);
window.addEventListener('pagehide', () => { clearTimeout(model.timer); model.detailController?.abort(); });
setInterval(renderActions, 1000);
renderEmpty(); poll();
