import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {topic} from '../constants/project.js';
import {esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

const STATUS_LABELS = {recorded:'已入账', duplicate:'重复补传', conflict:'日期冲突', month_closed:'月结拒收'};

function notify(text) {
  const message = document.getElementById('message');
  if (message) { message.textContent = text; message.className = 'ok'; }
}

function ingestSample() {
  const today = new Date().toISOString().slice(0,10);
  return JSON.stringify([{slip_id:'SM-NEW-01', entered_on:today}, {slip_id:'SM-NEW-02', entered_on:today}], null, 2);
}

export function ledgerPage(state) {
  const {ledger, orders, session} = state;
  if (!ledger) return heading(topic.modules[4]) + '<p class="empty">账簿加载中。</p>';
  const operator = session.role==='operator';
  const metrics = Object.entries(ledger.totals).map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
  const slips = ledger.slips.map(row=>[esc(row.slip_id), esc(row.entered_on), esc(row.location), esc(row.gate), esc(row.batch_id), esc(row.recorded_by), esc(row.updated_at)]);
  const months = ledger.months.map(row=>[esc(row.month), esc(row.partition), row.slips, row.closed?'<span class="status" data-state="failed">已月结</span>':'<span class="status" data-state="complete">在册</span>']);
  const history = orders.map(row=>[esc(row.order_id), esc(row.slip_id), esc(row.from_date), esc(row.to_date), esc(row.actor), esc(row.created_at)]);
  const pending = ledger.pending.length
    ? `<p class="note">待核池 ${ledger.pending.length} 张：${ledger.pending.map(row=>esc(row.slip_id)).join('、')}。登记对应月分区后自动回补。</p>` : '';
  const brief = `<h2>交班核对</h2><dl><dt>今天</dt><dd>${esc(ledger.today)}</dd><dt>账实一致</dt><dd>${ledger.consistent?'是':'否，请复核'}</dd>
    <dt>已月结</dt><dd>${esc(ledger.closed_months.join('、') || '无')}</dd></dl><p class="note">凭条去向以账簿物理分区为准。</p>`;
  const forms = operator ? `<div class="field-grid">
    <section class="panel"><h2>闸机补传</h2><form id="ingest-form">
      <label class="field"><span>闸机</span><input name="gate" required maxlength="40" value="东岗" /></label>
      <label class="field"><span>批次号</span><input name="batch_id" required maxlength="64" value="BG-${esc(ledger.today.replaceAll('-',''))}" /></label>
      <label>凭条清单（JSON 数组，含 slip_id 与 entered_on）<textarea name="rows" rows="6" required>${esc(ingestSample())}</textarea></label>
      <div class="actions form-actions"><button>补传入账</button></div></form></section>
    <section class="panel"><h2>凭条更正</h2><form id="correct-form">
      <label class="field"><span>更正单号</span><input name="order_id" required maxlength="64" placeholder="例如 CJ-20261121-01" /></label>
      <label class="field"><span>凭条编号</span><input name="slip_id" required maxlength="64" /></label>
      <label class="field"><span>更正后入园日</span><input name="corrected_on" type="date" required /></label>
      <div class="actions form-actions"><button>提交更正</button></div>
      <p class="note">更正单号为幂等键：回执丢失后重试同一笔，原样返回当时的回执。</p></form></section>
    <section class="panel"><h2>登记月分区</h2><form id="partition-form">
      <label class="field"><span>月份（月初日期）</span><input name="month" type="date" required /></label>
      <div class="actions form-actions"><button>挂载分区并回补</button></div>
      <p class="note">未建分区的月份，凭条先进入 DEFAULT 待核池。</p></form></section>
    <section class="panel"><h2>月结</h2><form id="close-form">
      <label class="field"><span>月结月份（月初日期）</span><input name="month" type="date" required /></label>
      <div class="actions form-actions"><button>办理月结</button></div>
      <p class="note">月结只锁本月份；完成后该月凭条不得迁入或迁出。</p></form></section>
  </div>` : '';
  return `${heading(topic.modules[4], ledger.summary, '<button class="secondary" type="button" id="ledger-refresh">刷新账簿</button>')}
    <div class="spread evidence-top"><dl class="metrics">${metrics}</dl><aside class="brief">${brief}</aside></div>
    ${panel('凭条账簿·逐张去向', records(['凭条编号','入园日期','去向','闸机','批次','经办','更新时间'], slips, '账簿暂无凭条，经办员可从闸机补传。') + pending)}
    ${panel('月分区', records(['月份','分区表','张数','状态'], months, '尚未登记月分区。'))}
    ${panel('更正单', records(['更正单号','凭条编号','原入园日','更正后','经办','时间'], history, '暂无更正单。'))}
    ${forms}`;
}

export function bindLedger(state, api, refresh, fail) {
  document.getElementById('ledger-refresh')?.addEventListener('click', refresh);
  submit('ingest-form', async data=>{
    let rows;
    try { rows = JSON.parse(data.get('rows')); }
    catch { throw new Error('凭条清单须为 JSON 数组。'); }
    const result = await api('/ledger/ingest', {method:'POST', data:{batch_id:data.get('batch_id'), gate:data.get('gate'), rows}});
    await refresh();
    const parts = Object.entries(result.summary).filter(([,n])=>n).map(([key,n])=>`${STATUS_LABELS[key]} ${n} 张`);
    notify(`批次 ${result.batch_id} 已提交：${parts.join('，') || '无凭条'}。`);
  }, fail);
  submit('correct-form', async data=>{
    const result = await api('/ledger/corrections', {method:'POST',
      data:{order_id:data.get('order_id'), slip_id:data.get('slip_id'), corrected_on:data.get('corrected_on')}});
    await refresh();
    notify(result.replayed
      ? `更正单 ${result.order_id} 此前已落库，返回当时回执：${result.slip_id} ${result.from_date} → ${result.to_date}（${result.to_location}）。`
      : `更正单 ${result.order_id} 已落库：${result.slip_id} ${result.from_date} → ${result.to_date}，去向 ${result.to_location}。`);
  }, fail);
  submit('partition-form', async data=>{
    const result = await api('/ledger/partitions', {method:'POST', data:{month:data.get('month')}});
    await refresh();
    notify(result.attached ? `已挂载 ${result.month} 分区，从待核池回补 ${result.moved} 张。` : `月份 ${result.month} 分区已在册。`);
  }, fail);
  submit('close-form', async data=>{
    const result = await api('/ledger/close', {method:'POST', data:{month:data.get('month')}});
    await refresh();
    notify(result.already_closed ? `月份 ${result.month} 此前已月结。` : `月份 ${result.month} 月结完成，该月凭条不得迁入或迁出。`);
  }, fail);
}
