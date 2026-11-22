import {feedback, field} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {BATCH, RECEIPT} from '../constants/enums.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';

const short = hex => hex ? `${hex.slice(0, 10)}…${hex.slice(-6)}` : '—';
const receiptPill = row => pill(RECEIPT[row.state] || row.state, row.state);

export async function financePage(route, refresh) {
  const {spec, runs} = workspace;
  const code = spec.finance_batch_code;
  const message = feedback();
  const deliverable = runs.filter(run => run.status === 'complete' && run.has_export);
  const reconcile = await workspace.financeReconcile(code);

  const demoSteps = el('ol', {class:'method-list finance-steps'});
  const replay = steps => demoSteps.replaceChildren(...steps.map(step =>
    el('li', {}, el('strong', {}, step.title), p(step.detail, 'subtle'))));

  const runDemo = button('一键模拟财务接收全流程', {class:'primary', 'data-testid':'finance-demo'});
  runDemo.addEventListener('click', handler(runDemo, message, async () => {
    const result = await workspace.financeDemo();
    replay(result.steps);
    message.textContent = '已重置并演完整批换版，下方对账已刷新。';
    await refresh();
  }));
  const demoControl = session.canReview
    ? runDemo
    : p('只读角色可查看对账结果；换版、接收与整批生效需复核主管操作。', 'subtle');

  const summary = reconcile.batches.length ? metrics([
    {label:'当前生效版本', value: reconcile.active_seq ? `v${reconcile.active_seq}` : '无'},
    {label:'换版批次', value: reconcile.batches.length},
    {label:'指纹不符', value: reconcile.batches.reduce((n, b) => n + b.mismatch_count, 0)},
    {label:'乱序回执', value: reconcile.batches.reduce((n, b) => n + b.stale_count, 0)},
  ]) : empty('还没有财务接收记录，点击上方按钮开始模拟。');

  const slotColumns = [
    {label:'报告', render: row => link(row.report_name, `#results/${row.run_id}`)},
    {key:'delivered_version', label:'交付版本'},
    {label:'回执', render: receiptPill},
    {label:'交付指纹', render: row => p(short(row.expected_checksum), 'filename')},
    {label:'实收指纹', render: row => p(short(row.actual_checksum), 'filename')},
    {label:'指纹', render: row => pill(row.checksum_ok ? '相符' : '不符', row.checksum_ok ? 'confirmed' : 'mismatch')},
    {key:'attempts', label:'重试次数'},
  ];

  const batchPanels = reconcile.batches.map(batch => panel(
    `批次 v${batch.seq} · ${BATCH[batch.state]}`,
    pill(BATCH[batch.state], batch.state),
    p(`应收 ${batch.expected_slots} 份 · 实收 ${batch.received_count} 份`
      + `${batch.archived_count ? ` · 随版归档 ${batch.archived_count} 份` : ''}`
      + ` · 指纹不符 ${batch.mismatch_count} 份 · 乱序 ${batch.stale_count} 份`,
      'subtle'),
    batch.error && p(batch.error, 'feedback'),
    table(slotColumns, batch.slots, {caption:`v${batch.seq} 对账槽位`, empty:'本批次暂无在用回执。', pageSize: 8}),
    batch.stale.length && panel('乱序 / 迟到回执（单列，不影响已确认版本）',
      table([...slotColumns, {key:'note', label:'说明'}], batch.stale,
        {caption:'乱序回执', empty:'无乱序回执。', pageSize: 8})),
  ));

  const manual = session.canReview ? manualPanel(deliverable, code, message, refresh) : null;

  return el('div', {class:'two-col'},
    el('div', {class:'wide-panel'},
      panel(spec.pages[6], p(spec.finance, 'intro-copy'), summary, demoControl, demoSteps, message),
      panel('报告交付对账（按实收版本与完整文件指纹）', ...(batchPanels.length ? batchPanels : [empty('暂无对账数据。')])),
    ),
    el('aside', {class:'sidebar-panel'},
      manual,
      panel('整批换版规则', el('ul', {class:'method-list'}, [
        '按批次整批换版：seq 单调递增，active 任一时刻至多一个，激活在单事务内原子完成。',
        '按交付版本与完整 SHA-256 指纹验收，财务端对实收字节重算指纹后再核对。',
        '已入库却没回执时沿用原文件重试，只累加 attempts，不重复入库。',
        '未补齐或指纹不符则挂起并保留旧版，重开补齐后再整批生效。',
        '迟到的旧批次/乱序旧版本回执一律记为 stale，不能覆盖或倒退新版。',
      ].map(text => el('li', {}, text)))),
    ));
}

function manualPanel(deliverable, code, message, refresh) {
  const runOptions = deliverable.map(run => el('option', {value: run.id}, `${run.name}（#${run.id}）`));
  const runSelect = el('select', {name:'run_id', required: true}, runOptions);
  const tamper = el('input', {name:'tamper', type:'checkbox'});
  const versionInput = el('input', {name:'delivered_version', type:'text', placeholder:'留空使用规范版本 v{seq}.r{run}', maxlength: 40});
  const openBtn = button('开换版批次', {type:'submit', class:'primary'});
  const openForm = el('form', {class:'fields'},
    field({key:'batch_code', label:'批次号', type:'string', max_length:120}, code),
    field({key:'expected_slots', label:'应收报告份数', type:'int', min:1, max:50}, 3),
    openBtn);
  openForm.addEventListener('submit', handler(openBtn, message, async event => {
    const data = new FormData(event.target);
    await workspace.financeOpen({batch_code: data.get('batch_code'), expected_slots: Number(data.get('expected_slots'))});
    message.textContent = '换版批次已建立，状态为待补齐。';
    await refresh();
  }));

  const batchSelect = el('select', {name:'batch_id'});
  const receiveBtn = button('验收并接收所选报告', {class:'quiet'});
  const activateBtn = button('整批生效', {class:'primary'});
  const reopenBtn = button('重开挂起批次', {class:'quiet'});
  const receiveForm = el('form', {class:'fields'},
    el('label', {class:'field'}, el('span', {}, '选择换版批次'), batchSelect),
    el('label', {class:'field'}, el('span', {}, '选择交付报告'), runSelect),
    el('label', {class:'check'}, tamper, '模拟实收文件被篡改（指纹不符）'),
    el('label', {class:'field'}, el('span', {}, '交付版本（可留空）'), versionInput),
    el('div', {class:'actions'}, receiveBtn, activateBtn, reopenBtn));
  // 该面板的操作都走显式按钮，拦截回车导致的原生提交。
  receiveForm.addEventListener('submit', event => event.preventDefault());

  async function syncBatches() {
    const batches = await workspace.financeBatches();
    batchSelect.replaceChildren(...batches.map(b =>
      el('option', {value:b.id}, `${b.batch_code} v${b.seq} · ${BATCH[b.state]}`)));
  }
  syncBatches();

  receiveBtn.addEventListener('click', handler(receiveBtn, message, async () => {
    const body = {run_id: Number(runSelect.value), tamper: tamper.checked};
    const version = versionInput.value.trim();
    if (version) body.delivered_version = version;
    const receipt = await workspace.financeReceive(Number(batchSelect.value), body);
    message.textContent = `回执：${RECEIPT[receipt.state]}（重试 ${receipt.attempts} 次）`;
    await refresh();
  }));
  activateBtn.addEventListener('click', handler(activateBtn, message, async () => {
    const batch = await workspace.financeActivate(Number(batchSelect.value));
    message.textContent = `批次 v${batch.seq} 已整批生效。`;
    await refresh();
  }));
  reopenBtn.addEventListener('click', handler(reopenBtn, message, async () => {
    const batch = await workspace.financeReopen(Number(batchSelect.value));
    message.textContent = `批次 v${batch.seq} 已重开，可继续补齐。`;
    await refresh();
  }));

  return panel('手工模拟接收',
    p('用于逐步验证：开批次 → 接收（可勾选篡改）→ 整批生效 / 重开补齐。', 'subtle'),
    openForm, el('hr'), receiveForm);
}
