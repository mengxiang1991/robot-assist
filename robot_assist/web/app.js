'use strict';
const $ = id => document.getElementById(id);
const formValue = (form, key) => form.elements.namedItem(key).value;
const checked = (form, key) => form.elements.namedItem(key).checked;
const dates = value => value.trim().split(/\s+/).filter(Boolean);
let token = sessionStorage.getItem('robot-assist-token') || '';
let currentPage = 'home';
let currentRun = '';
let configs = [];

const templates = {
  model: {url:'https://example.test/v1', model:'example-model', token_env:'ROBOT_ASSIST_MODEL_KEY', timeout:45},
  tool: {type:'mcp', transport:'http', url:'https://example.test/mcp', operation:'search', effect:'read'},
  flow: {name:'示例 Flow', executor:'flow', model:'my-model', tools:[], steps:[
    {type:'model', instruction:'根据输入生成摘要。'}
  ]},
  market: {url:'https://example.test/robot-assist-catalog.json', token_env:'ROBOT_ASSIST_MARKET_KEY'}
};

async function request(path, payload) {
  const headers = token ? {Authorization:'Bearer ' + token} : {};
  const options = {headers};
  if (payload !== undefined) {
    options.method = 'POST';
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(payload);
  }
  const response = await fetch('/api/' + path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || ('HTTP ' + response.status));
  return result;
}

function message(text, error = false) {
  $('status').textContent = text;
  $('status').dataset.error = String(error);
}

function action(callback) {
  return async event => {
    event.preventDefault();
    const control = event.submitter || event.currentTarget;
    control.disabled = true;
    message('');
    try {
      await callback(event);
    } catch (error) {
      message(error.message, true);
    } finally {
      control.disabled = false;
    }
  };
}

function record(target, text, detail, label, handler) {
  const row = document.createElement('div');
  row.className = 'record';
  const heading = document.createElement('div');
  const name = document.createElement('b');
  name.textContent = text;
  heading.append(name, document.createElement('br'));
  const meta = document.createElement('small');
  meta.textContent = detail;
  heading.append(meta);
  row.append(heading);
  if (label) {
    const button = document.createElement('button');
    button.textContent = label;
    button.className = 'secondary';
    button.addEventListener('click', action(handler));
    row.append(button);
  }
  target.append(row);
}

function populate(form, name, kind) {
  const control = form.elements.namedItem(name);
  const previous = control.value;
  control.replaceChildren(new Option('请选择', ''));
  configs.filter(item => item.kind === kind).forEach(item => {
    control.add(new Option(item.body.name || item.id, item.id));
  });
  if ([...control.options].some(option => option.value === previous)) control.value = previous;
}

async function loadConfigs() {
  configs = await request('config');
  populate($('task'), 'flow', 'flow');
  const bookingFlow = $('booking-form').elements.flow;
  const previousBookingFlow = bookingFlow.value;
  bookingFlow.replaceChildren(new Option('请选择预订 Flow', ''));
  configs.filter(item => item.kind === 'flow' && item.body.approval === 'booking').forEach(item => {
    bookingFlow.add(new Option(item.body.name || item.id, item.id));
  });
  bookingFlow.value = [...bookingFlow.options].some(option => option.value === previousBookingFlow)
    && previousBookingFlow ? previousBookingFlow :
    [...bookingFlow.options].some(option => option.value === 'demo-booking') ? 'demo-booking' : '';
  bookingMode();
  populate($('schedule-form'), 'flow', 'flow');
  populate($('market-form'), 'id', 'market');
  $('configs').replaceChildren();
  if (!configs.length) $('configs').textContent = '还没有配置。';
  for (const item of configs) {
    record($('configs'), item.id, item.kind, '编辑', async () => editConfig(item));
    if (item.kind === 'model' || item.kind === 'tool') {
      const row = $('configs').lastElementChild;
      const test = document.createElement('button');
      test.className = 'secondary';
      test.textContent = item.kind === 'model' ? '测试连接' : '测试工具';
      test.addEventListener('click', action(async () => {
        const input = item.kind === 'tool' && item.body.type !== 'mcp'
          ? prompt('输入只读工具的样例 JSON。', '{}') : '{}';
        if (input === null) return;
        const output = await request(item.kind + '/test', {id:item.id,input:JSON.parse(input)});
        $('config-test-result').hidden = false;
        $('config-test-result').textContent = JSON.stringify(output, null, 2);
        message('测试已完成。');
      }));
      row.append(test);
    }
  }
}

function editConfig(item) {
  const fields = $('config-form').elements;
  fields.kind.value = item.kind;
  fields.id.value = item.id;
  fields.body.value = JSON.stringify(item.body, null, 2);
  changePage('config', false);
}

async function loadSchedules() {
  $('schedules').replaceChildren();
  const items = await request('schedules');
  if (!items.length) $('schedules').textContent = '暂无计划。';
  for (const item of items) {
    record($('schedules'), item.id, [item.flow, item.time, item.timezone,
      item.last_slot ? '上次 ' + item.last_slot : '尚未触发'].join(' · '), item.enabled ? '暂停' : '启用', async () => {
      await request('schedules/toggle', {id:item.id, enabled:!item.enabled});
      await loadSchedules();
    });
  }
}

const statusLabels = {queued:'排队', running:'运行中', succeeded:'已结束', failed:'失败', unknown:'待核验'};
function statusName(key) { return statusLabels[key] || key; }

async function loadRun(id) {
  const run = await request('runs/' + id);
  if (id !== currentRun) return;
  $('run-detail').hidden = false;
  $('run-result').textContent = JSON.stringify({...run, status:statusName(run.status)}, null, 2);
  $('approve').hidden = !run.approvable;
}

async function loadRuns() {
  const rows = await request('runs');
  $('run-list').replaceChildren();
  if (!rows.length) $('run-list').textContent = '暂无运行。';
  for (const run of rows) {
    record($('run-list'), statusName(run.status) + ' · ' + run.id.slice(0, 10),
      new Date(run.created * 1000).toLocaleString() + (run.error ? ' · ' + run.error : ''), '详情', async () => {
        currentRun = run.id;
        await loadRun(run.id);
      });
  }
  if (currentRun) await loadRun(currentRun);
}

function changePage(page, refresh = true) {
  currentPage = page;
  document.querySelectorAll('.page').forEach(el => {el.hidden = el.id !== page;});
  document.querySelectorAll('[data-page]').forEach(button => {
    button.setAttribute('aria-current', button.dataset.page === page ? 'page' : 'false');
    if (button.dataset.page === page) $('page-title').textContent = button.textContent;
  });
  if (refresh) refreshPage().catch(error => message(error.message, true));
}
async function refreshPage() {
  if (currentPage === 'runs') return loadRuns();
  await loadConfigs();
  if (currentPage === 'schedule') await loadSchedules();
}

document.querySelectorAll('[data-page],[data-open]').forEach(button => {
  button.addEventListener('click', () => {
    changePage(button.dataset.page || button.dataset.open);
    if (button.dataset.open === 'schedule') {
      const form = $('schedule-form');
      form.elements.input.value = JSON.stringify({room:'A301', slot:'10:00–11:00', date_offset_days:1}, null, 2);
      loadConfigs().then(() => {form.elements.flow.value = 'demo-booking';})
        .catch(error => message(error.message, true));
    }
  });
});
$('auth').addEventListener('click', () => {
  const value = prompt('填写服务访问令牌。本机默认可留空。', '');
  if (value === null) return;
  token = value;
  sessionStorage.setItem('robot-assist-token', token);
  refreshPage().catch(error => message(error.message, true));
});
const configForm = $('config-form');
configForm.elements.kind.addEventListener('change', () => {
  configForm.elements.body.value = JSON.stringify(templates[configForm.elements.kind.value], null, 2);
});
configForm.elements.kind.dispatchEvent(new Event('change'));
configForm.addEventListener('submit', action(async () => {
  await request('config', {
    kind:formValue(configForm, 'kind'), id:formValue(configForm, 'id'),
    body:JSON.parse(formValue(configForm, 'body'))
  });
  await loadConfigs();
  message('配置已保存。');
}));

$('market-form').addEventListener('submit', action(async () => {
  const selectedMarket = formValue($('market-form'), 'id');
  const items = await request('market/browse', {id:selectedMarket});
  $('market-items').replaceChildren();
  if (!items.length) $('market-items').textContent = '目录没有条目。';
  for (const item of items) {
    record($('market-items'), item.name || item.id, item.version, '导入草稿', async () => {
      const body = await request('market/import', {
        source:selectedMarket, item:item.id, version:item.version
      });
      editConfig({id:(item.id + '-' + item.version).replace(/[^a-zA-Z0-9_-]/g, '-'), kind:'tool', body});
      message('已填入市场版本配置。检查工具路径、凭据和读写权限后再保存。');
    });
  }
}));

async function enqueue(payload) {
  const result = await request('runs', payload);
  currentRun = result.runs[0];
  changePage('runs', false);
  await loadRuns();
  message('已提交 ' + result.runs.length + ' 项。');
}

const task = $('task');
task.addEventListener('submit', action(async () => {
  await enqueue({
    flow:formValue(task, 'flow'), input:JSON.parse(formValue(task, 'input')),
    commit:checked(task, 'commit')
  });
}));

const booking = $('booking-form');
function bookingMode() {
  const batch = formValue(booking, 'mode') === 'batch';
  $('batch-dates').hidden = !batch;
  $('single-date').hidden = batch;
  booking.elements.date.required = !batch &&
    (booking.elements.flow.value === 'demo-booking' || !booking.elements.prompt.value.trim());
  booking.elements.dates.required = batch;
}
booking.elements.mode.addEventListener('change', bookingMode);
booking.elements.flow.addEventListener('change', bookingMode);
booking.elements.prompt.addEventListener('input', bookingMode);
bookingMode();
booking.addEventListener('submit', action(async () => {
  const batch = formValue(booking, 'mode') === 'batch';
  const input = {room:formValue(booking,'room'), slot:formValue(booking,'slot')};
  const day = formValue(booking,'date');
  if (!batch && day) input.date = day;
  const prompt = formValue(booking,'prompt').trim();
  if (prompt) input.prompt = prompt;
  await enqueue({flow:formValue(booking,'flow'), input,
    dates:batch ? dates(formValue(booking,'dates')) : [], commit:false});
}));

const schedule = $('schedule-form');
schedule.addEventListener('submit', action(async () => {
  await request('schedules', {
    id:formValue(schedule,'id'), flow:formValue(schedule,'flow'),
    time:formValue(schedule,'time'), timezone:formValue(schedule,'timezone'),
    input:JSON.parse(formValue(schedule,'input')), dates:dates(formValue(schedule,'dates')),
    enabled:checked(schedule,'enabled'), commit:checked(schedule,'commit')
  });
  await loadSchedules();
  message('计划已保存。');
}));

$('approve').addEventListener('click', action(async () => {
  const result = await request('runs/' + currentRun + '/approve', {});
  currentRun = result.run;
  await loadRuns();
  message('已提交预订，执行时会再次核对可用性。');
}));
$('refresh').addEventListener('click', action(loadRuns));
changePage('home');
const poll = async () => {
  if (currentPage === 'runs' && !document.hidden) {
    try { await loadRuns(); } catch (error) {message(error.message, true);}
  }
  setTimeout(poll, 2300);
};
setTimeout(poll, 2300);
