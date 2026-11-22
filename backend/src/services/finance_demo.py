"""财务接收演示编排：在数据库内确定性地演完整批换版的全部规则。

每个步骤都调用 src.services.finance 的正式逻辑，演示与生产共用同一套幂等、
指纹核验、整批原子换版与乱序防护代码，仅在此固定输入与顺序。
"""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from scripts.seed_demo import FINANCE_RUN_SPECS
from src.constants.enums import BatchState, ReceiptState
from src.domain.spec import SPEC
from src.models.finance import FinanceBatch, FinanceReceipt
from src.models.jobs import Run
from src.services import finance
from src.services.finance_packets import canonical_version, load_packet


def reset(db: Session) -> None:
    """清掉财务演示数据，保留底层运行与导出文件，便于重复演示。"""
    db.execute(delete(FinanceReceipt))
    db.execute(delete(FinanceBatch))
    db.flush()


def _runs(db: Session) -> list[Run]:
    runs = []
    for name, _params, _review in FINANCE_RUN_SPECS:
        run = db.scalar(select(Run).where(Run.name == name).order_by(Run.id.desc()).limit(1))
        if run is None:
            raise RuntimeError('请先执行 seed_demo 生成财务交付运行')
        runs.append(run)
    return runs


def _packet(db: Session, batch: FinanceBatch, run: Run, tamper=False, version_override=''):
    return load_packet(db, batch.seq, run.id, tamper, version_override)


def run_demo(db: Session, actor: str = 'admin') -> dict:
    code = SPEC['finance_batch_code']
    reset(db)
    r1, r2, r3 = _runs(db)
    steps = []

    def log(title, detail, **marks):
        entry = {'title': title, 'detail': detail}
        entry.update(marks)
        steps.append(entry)
        return entry

    # ① 批次 v1：三份报告整批换版，全部指纹相符后整批生效。
    b1 = finance.open_batch(db, code, expected_slots=3, actor=actor)
    for run in (r1, r2, r3):
        packet = _packet(db, b1, run)
        finance.receive_report(db, b1, run, packet.delivered_version,
                               packet.payload, packet.expected_checksum, actor)
    finance.activate_batch(db, b1, actor)
    log('① 批次 v1 整批换版', '3 份报告按交付版本与完整指纹验收通过，旧版（无）→ v1 整批生效',
        batch_seq=1, state=BatchState.ACTIVE)

    # ② 已入库却没回执：沿用原文件重试，幂等不重复入库。
    packet = _packet(db, b1, r1)
    before = len(db.scalars(select(FinanceReceipt).where(FinanceReceipt.batch_id == b1.id)).all())
    outcome = finance.receive_report(db, b1, r1, packet.delivered_version,
                                     packet.payload, packet.expected_checksum, actor)
    after = len(db.scalars(select(FinanceReceipt).where(FinanceReceipt.batch_id == b1.id)).all())
    log('② 无回执重试（幂等）', f'沿用原文件重发 {r1.name}，回执数 {before}→{after}（不重复入库），attempts={outcome.receipt.attempts}',
        replayed=outcome.replayed, attempts=outcome.receipt.attempts, receipt_count=after)

    # ③ 批次 v2 写到一半：先收到一份指纹不符的回执，批次不得生效，v1 保留。
    b2 = finance.open_batch(db, code, expected_slots=3, actor=actor)
    bad = _packet(db, b2, r1, tamper=True)
    finance.receive_report(db, b2, r1, bad.delivered_version, bad.payload, bad.expected_checksum, actor)
    good = _packet(db, b2, r2)
    finance.receive_report(db, b2, r2, good.delivered_version, good.payload, good.expected_checksum, actor)
    blocked = None
    try:
        finance.activate_batch(db, b2, actor)
    except finance.BatchActivationError as error:
        blocked = str(error)
    log('③ 半成品换版被拦截', f'v2 仅到 2/3 且含 1 份指纹不符，拒绝整批生效：{blocked}；v2 挂起，v1 继续保持 active',
        state=BatchState.FAILED, blocked=blocked)

    # ④ 补齐 v2：先重开挂起批次，重发正确文件修正指纹，补第三份报告后整批生效，v1 降为 superseded。
    finance.reopen_batch(db, b2, actor)
    fixed = _packet(db, b2, r1)
    repair = finance.receive_report(db, b2, r1, fixed.delivered_version,
                                    fixed.payload, fixed.expected_checksum, actor)
    third = _packet(db, b2, r3)
    finance.receive_report(db, b2, r3, third.delivered_version, third.payload, third.expected_checksum, actor)
    finance.activate_batch(db, b2, actor)
    log('④ 补齐后整批生效', f'重开挂起批次，原指纹不符槽位重发正确文件修正（attempts={repair.receipt.attempts}），3/3 齐全后 v2 生效，v1 整批降为 superseded',
        batch_seq=2, state=BatchState.ACTIVE)

    # ⑤ 迟到的旧报告：向已被取代的 v1 重发旧版本，记为 stale，不覆盖 v2。
    old_packet = _packet(db, b1, r3, version_override=canonical_version(b1.seq, r3.id))
    late = finance.receive_report(db, b1, r3, old_packet.delivered_version,
                                  old_packet.payload, old_packet.expected_checksum, actor)
    log('⑤ 迟到旧报告不覆盖新版', f'v1 已 superseded，旧版本回执记为 {ReceiptState.STALE}，v2 已确认版本不倒退',
        stale=late.receipt.state == ReceiptState.STALE)

    # ⑥ 乱序回执：当前 active 槽位已确认后再塞旧版本，同样记为 stale。
    disorder = _packet(db, b2, r2, version_override=f'v{b2.seq - 1}.r{r2.id}')
    late2 = finance.receive_report(db, b2, r2, disorder.delivered_version,
                                   disorder.payload, disorder.expected_checksum, actor)
    log('⑥ 乱序回执不倒退确认版本', f'{r2.name} 在 v2 已确认，旧版本回执记为 {ReceiptState.STALE}，已确认版本保持不变',
        stale=late2.receipt.state == ReceiptState.STALE)

    # ⑦ 批次 v3 写一半失败：只到 1/3，激活失败并挂起，保留 v2 为当前生效版。
    b3 = finance.open_batch(db, code, expected_slots=3, actor=actor)
    p = _packet(db, b3, r1)
    finance.receive_report(db, b3, r1, p.delivered_version, p.payload, p.expected_checksum, actor)
    blocked3 = None
    try:
        finance.activate_batch(db, b3, actor)
    except finance.BatchActivationError as error:
        blocked3 = str(error)
    log('⑦ 新版写一半失败保留旧版', f'v3 仅到 1/3，激活被拦截并挂起：{blocked3}；active 仍是 v2',
        state=BatchState.FAILED, blocked=blocked3)

    return {'batch_code': code, 'steps': steps,
            'reconciliation': finance.reconciliation(db, code)}
