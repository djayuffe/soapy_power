'use strict';

// Shares the console's authenticated request function. Values stay typed;
// command text shown in the preview is never sent back for execution.
const workbench = {catalog: null, command: null, job: null, plan: null, revision: 0, timer: null, busy: false};
const wb = id => document.getElementById(id);
const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};

async function openWorkbench() {
  wb('workbench-dialog').showModal();
  if (!workbench.catalog) {
    try {
      workbench.catalog = await request('/api/options');
      const select = wb('expert-operation');
      for (const command of workbench.catalog.commands) {
        const option = el('option', command.id.replaceAll(':', ' / '));
        option.value = command.id;
        select.append(option);
      }
      renderExpert();
      await refreshArtifacts();
    } catch (error) { expertError(error); }
  }
  pollExpert();
}

function expertError(error) {
  wb('expert-error').textContent = error.message || String(error);
  wb('expert-error').hidden = false;
}

function invalidateExpert() {
  workbench.revision++;
  workbench.plan = null;
  wb('expert-run').disabled = true;
  wb('expert-preview').textContent = 'Validate the current settings to review the execution plan.';
}

function renderExpert() {
  workbench.satellite=null;
  const command = workbench.catalog.commands.find(c => c.id === wb('expert-operation').value);
  workbench.command = command;
  wb('expert-description').textContent = command.description;
  wb('expert-acquisition').hidden = command.id !== 'acquisition';
  const target = wb('expert-fields');
  target.replaceChildren();
  const sections = new Map();
  for (const option of command.options) {
    if (['managed', 'output'].includes(option.role)) continue;
    let group = option.group;
    if (command.id === 'acquisition') {
      const name = option.dest;
      group = ['freq', 'centers', 'rate', 'bandwidth', 'device', 'antenna', 'channel', 'gain', 'specific_gains', 'agc', 'bins', 'bin_size', 'time', 'total_time', 'repeats'].includes(name) ? 'Receiver and scan geometry'
        : /dc|iq|window|overlap|crop|coverage|detrend|calibration|profile|halfwidth/.test(name) ? 'DSP, calibration and spur prevention'
        : /lock|headroom|telemetry|settle|rf_state|timestamp|partial|delay|deadline/.test(name) ? 'Qualification and telemetry'
        : 'Driver, clock and performance';
    }
    if (!sections.has(group)) {
      const details = el('details');
      details.open = command.id !== 'acquisition' || sections.size === 0;
      details.append(el('summary', group));
      const grid = el('div', undefined, 'expert-grid');
      details.append(grid);
      target.append(details);
      sections.set(group, grid);
    }
    const label = el('label', undefined, 'expert-field');
    label.dataset.search = `${option.dest} ${option.help}`.toLowerCase();
    label.append(el('span', option.dest.replaceAll('_', ' ') + (option.required ? ' *' : '')));
    let input;
    if (option.kind === 'boolean' || option.choices) {
      input = el('select');
      const unset = el('option', `Default${option.default !== null ? ': ' + option.default : ''}`);
      unset.value = '';
      input.append(unset);
      const choices = option.kind === 'boolean' ? [true, false] : option.choices;
      for (const choice of choices) {
        const item = el('option', String(choice)); item.value = String(choice); input.append(item);
      }
    } else {
      input = el('input');
      input.type = ['integer', 'number'].includes(option.kind) && !['+', '*'].includes(option.nargs) ? 'number' : 'text';
      if (input.type === 'number') input.step = option.kind === 'integer' ? '1' : 'any';
      input.placeholder = ['input', 'source'].includes(option.role) ? 'Session-relative artifact path'
        : option.nargs === '+' ? 'Space-separated values or JSON array'
        : option.default === null ? 'Not set' : String(option.default);
      if (option.role === 'input') input.setAttribute('list', 'expert-artifacts');
    }
    input.dataset.dest = option.dest;
    input.autocomplete = 'off';
    input.addEventListener('input', () => {if(['freq','centers'].includes(option.dest)&&workbench.satellite){workbench.satellite=null;wb('expert-description').textContent='Frequency edited; previous SatNOGS target declaration cleared.';}invalidateExpert();});
    label.append(input, el('small', option.help || (option.role === 'input' ? 'Existing artifact inside the sessions directory.' : option.flags.join(', '))));
    sections.get(group).append(label);
  }
  wb('expert-managed').textContent = 'Managed by runner: ' + command.options.filter(o => ['managed', 'output'].includes(o.role)).map(o => o.dest).join(', ');
  invalidateExpert();
}

function expertPayload() {
  const values = {};
  for (const input of wb('expert-fields').querySelectorAll('[data-dest]')) {
    if (input.value === '') continue;
    const option = workbench.command.options.find(o => o.dest === input.dataset.dest);
    let value = input.value;
    if (['+', '*'].includes(option.nargs) || Number.isInteger(option.nargs) && option.nargs > 1) {
      value = value.trim().startsWith('[') ? JSON.parse(value) : value.trim().split(/\s+/);
      if (['integer', 'number'].includes(option.kind)) value = value.map(Number);
    } else if (option.kind === 'boolean') value = value === 'true';
    else if (['integer', 'number'].includes(option.kind)) value = Number(value);
    values[option.dest] = value;
  }
  return {...(workbench.satellite?{satellite:workbench.satellite}:{}), command: workbench.command.id, values, runtime: wb('expert-runtime').value,
    seconds: Number(wb('expert-seconds').value),
    anti_spur: workbench.command.id === 'acquisition' && wb('expert-antispur').checked};
}

async function validateExpert() {
  wb('expert-error').hidden = true;
  wb('expert-run').disabled = true;
  const revision = workbench.revision;
  try {
    const payload = expertPayload();
    const result = await request('/api/plan', payload);
    if (revision !== workbench.revision) return;
    workbench.plan = payload;
    wb('expert-preview').textContent = JSON.stringify(result, null, 2);
    wb('expert-run').disabled = workbench.busy;
  } catch (error) { expertError(error); }
}

async function runExpert() {
  if (!workbench.plan || workbench.busy) return;
  workbench.busy = true;
  wb('expert-run').disabled = true;
  try {
    const result = await request('/api/expert', workbench.plan);
    workbench.job = result.job_id;
    try { localStorage.setItem('soapyExpertJob', workbench.job); } catch { /* storage is optional */ }
    wb('expert-job-id').value = workbench.job;
    wb('expert-job-state').textContent = 'Queued · waiting for the serial runner';
    workbench.plan = null;
    wb('expert-preview').textContent = 'Queued successfully. Validate again before submitting another job.';
    await pollExpert();
  } catch (error) { expertError(error); }
  finally { workbench.busy = false; }
}

async function refreshArtifacts() {
  const result = await request('/api/artifacts');
  const list = wb('expert-artifacts'); list.replaceChildren();
  for (const item of result.items) {
    const option = el('option'); option.value = item.path; list.append(option);
  }
  wb('artifact-count').textContent = `${result.items.length} artifacts${result.truncated ? ' · listing capped; exact paths still accepted' : ''}`;
}

async function pollExpert() {
  clearTimeout(workbench.timer);
  if (!wb('workbench-dialog').open) return;
  const id = workbench.job;
  try {
    if (id) {
      const result = await request('/api/expert?id=' + encodeURIComponent(id));
      if (id !== workbench.job) return;
      wb('expert-job-state').textContent = `${result.id} · ${result.state} · active process time ${format(result.active_seconds)} s · ${result.attempts?.length || 0} attempts${result.abort_requested ? ' · abort requested' : result.pending_control ? ' · ' + result.pending_control + ' awaiting acknowledgement' : ''}`;
      wb('expert-log').textContent = result.log || result.error || 'Waiting for worker output…';
      const files = wb('expert-files'); files.replaceChildren();
      for (const path of result.files || []) {
        const link = el('a', path);
        link.href = '/artifacts/' + id + '/' + path.split('/').map(encodeURIComponent).join('/');
        link.target = '_blank'; link.rel = 'noopener'; files.append(link);
      }
      for (const action of ['pause', 'resume', 'retry', 'abort']) {
        const control = wb('expert-' + action);
        control.disabled = result.abort_requested || (result.pending_control && action !== 'abort') || !['running', 'paused'].includes(result.state) || (action !== 'abort' && result.command !== 'acquisition')
          || (action === 'resume' && result.state !== 'paused') || (action === 'pause' && result.state === 'paused');
      }
    }
  } catch (error) { expertError(error); }
  finally { workbench.timer = setTimeout(pollExpert, 2500); }
}

function setExpertPreset() {
  wb('expert-operation').value = 'acquisition';
  wb('expert-runtime').value = 'b200';
  renderExpert();
  const settings = {freq: '100M:200M', device: 'driver=uhd,type=b200,recv_frame_size=16360,num_recv_frames=1024',
    antenna: 'RX2', rate: '2M', bins: 8192, gain: 30, time: 0.25, overlap: 75,
    fft_window: 'blackman', dc_mask_width: 10000, ensure_coverage: true, rf_state: true,
    headroom: true, headroom_max: 0.5, headroom_p999: 0.2, partial_policy: 'reject', tune_delay: 0.05,
    max_threads: 2, max_queue_size: 4};
  for (const input of wb('expert-fields').querySelectorAll('[data-dest]')) {
    if (Object.hasOwn(settings, input.dataset.dest)) input.value = String(settings[input.dataset.dest]);
  }
  wb('expert-antispur').checked = true;
  invalidateExpert();
}

wb('workbench-nav').addEventListener('click', openWorkbench);
wb('expert-close').addEventListener('click', () => wb('workbench-dialog').close());
wb('workbench-dialog').addEventListener('close', () => clearTimeout(workbench.timer));
wb('expert-operation').addEventListener('change', renderExpert);
wb('expert-preset').addEventListener('click', setExpertPreset);
wb('expert-validate').addEventListener('click', validateExpert);
wb('expert-run').addEventListener('click', runExpert);
for (const id of ['expert-runtime', 'expert-seconds', 'expert-antispur']) wb(id).addEventListener('input', invalidateExpert);
wb('expert-search').addEventListener('input', event => {
  const query = event.target.value.toLowerCase();
  for (const field of wb('expert-fields').querySelectorAll('.expert-field')) {
    field.hidden = !field.dataset.search.includes(query);
    if (query && !field.hidden) field.closest('details').open = true;
  }
});
wb('expert-refresh-artifacts').addEventListener('click', () => refreshArtifacts().catch(expertError));
wb('expert-open-job').addEventListener('click', () => {
  const id = wb('expert-job-id').value.trim();
  if (!/^expert-\d{8}-\d{6}-[a-f0-9]{8}$/.test(id)) return expertError(new Error('Enter a valid expert job identifier'));
  workbench.job = id; pollExpert();
});
for (const action of ['pause', 'resume', 'retry', 'abort']) wb('expert-' + action).addEventListener('click', async event => {
  event.target.disabled = true;
  try {
    await request('/api/expert-control', {id: workbench.job, action});
    wb('expert-job-state').textContent = `${action} requested · awaiting worker acknowledgement`;
  } catch (error) { expertError(error); }
});
try {
  workbench.job = localStorage.getItem('soapyExpertJob');
  wb('expert-job-id').value = workbench.job || '';
} catch { /* Private browsing can disable storage; job IDs remain usable. */ }

wb('artifact-save').addEventListener('click', async event => {
  event.target.disabled = true;
  try {
    const result = await request('/api/artifact', {name: wb('artifact-name').value, data: JSON.parse(wb('artifact-json').value)});
    wb('artifact-created').textContent = result.path;
    await refreshArtifacts();
  } catch (error) { expertError(error); }
  finally { event.target.disabled = false; }
});

wb('open-expert-job').addEventListener('click', () => {
  workbench.job = model.selected;
  wb('expert-job-id').value = workbench.job;
  openWorkbench().catch(expertError);
});
