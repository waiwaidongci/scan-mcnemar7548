"""财务接收：按批次整批换版，按交付版本和完整文件指纹验收入库。

规则要点：
- 批次 seq 单调递增，active 批次任一时刻至多一个，整批在单个事务内换版。
- 接收时对实收字节重算 SHA-256，与交付侧指纹核对；不符则单列 mismatch，不得入库。
- (batch_id, run_id) 唯一。已入库却没回执时按原文件重试，只累加 attempts，不重复入库。
- 未补齐或存在指纹不符的批次不得激活，旧 active 批次保留；补齐后再整批生效。
- 迟到的旧批次/旧版本回执记为 stale，不覆盖、不倒退任何已确认版本。
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import BatchState, ReceiptState
from src.models.finance import FinanceBatch, FinanceReceipt
from src.models.jobs import Run
from src.services.audit import record
from src.utils.fingerprints import file_fingerprint
from src.utils.time import now


class BatchActivationError(Exception):
    """整批生效未达标：批次置为 failed 并保留旧版，异常把原因带回调用方。"""


@dataclass
class ReceiveOutcome:
    receipt: FinanceReceipt
    replayed: bool


def open_batch(db: Session, batch_code: str, expected_slots: int, actor: str) -> FinanceBatch:
    """开一张整批换版入库单；同 code 的 seq 必须高于历史，保证版本不倒退。"""
    previous_seq = db.scalar(
        select(FinanceBatch.seq).where(FinanceBatch.batch_code == batch_code)
        .order_by(FinanceBatch.seq.desc()).limit(1))
    seq = (previous_seq or 0) + 1
    if expected_slots < 1:
        raise ValueError('换版批次至少包含一个报告槽位')
    batch = FinanceBatch(batch_code=batch_code, seq=seq, state=BatchState.STAGING,
                         expected_slots=expected_slots, author=actor)
    db.add(batch)
    db.flush()
    record(db, actor, 'finance.batch.opened', batch.id, batch_code=batch_code, seq=seq,
           expected_slots=expected_slots)
    return batch


def _live_receipt(db: Session, batch: FinanceBatch, run: Run) -> FinanceReceipt | None:
    return db.scalar(select(FinanceReceipt).where(
        FinanceReceipt.batch_id == batch.id, FinanceReceipt.run_id == run.id,
        FinanceReceipt.state != ReceiptState.STALE).with_for_update())


def _mark_stale(db: Session, batch: FinanceBatch, run: Run, delivered_version: str,
                expected_checksum: str, actual: str, payload: bytes, actor: str, note: str,
                stamp) -> FinanceReceipt:
    # 迟到/乱序回执只留痕，不动既有槽位、不参与实收计数。
    late = FinanceReceipt(batch_id=batch.id, run_id=run.id, report_name=run.name,
                          delivered_version=delivered_version, expected_checksum=expected_checksum,
                          actual_checksum=actual, file_size=len(payload),
                          state=ReceiptState.STALE, attempts=1, note=note,
                          created_at=stamp, updated_at=stamp)
    db.add(late)
    db.flush()
    record(db, actor, 'finance.receipt.stale', late.id, batch_id=batch.id, run_id=run.id,
           delivered_version=delivered_version)
    return late


def receive_report(db: Session, batch: FinanceBatch, run: Run, delivered_version: str,
                   payload: bytes, expected_checksum: str, actor: str) -> ReceiveOutcome:
    """接收一份报告并验收指纹。所有判断在调用方事务内完成。"""
    actual = file_fingerprint(payload)
    matches = actual == expected_checksum
    stamp = now()

    # 批次已被新版取代：任何迟到投递都只留痕，绝不触碰已确认版本。
    if batch.state == BatchState.SUPERSEDED:
        stale = _mark_stale(db, batch, run, delivered_version, expected_checksum, actual,
                            payload, actor, '批次已被新版取代，迟到回执已忽略', stamp)
        return ReceiveOutcome(stale, replayed=False)
    if batch.state == BatchState.FAILED:
        raise ValueError('该换版批次已挂起，请先重开批次补齐后再整批生效')

    existing = _live_receipt(db, batch, run)

    if existing is not None and existing.state == ReceiptState.CONFIRMED:
        # 同文件同版本重试：已入库却没回执时幂等重放，不重复入库。
        same_file = existing.expected_checksum == expected_checksum and matches
        if existing.delivered_version == delivered_version and same_file:
            existing.attempts += 1
            existing.updated_at = stamp
            record(db, actor, 'finance.receipt.replayed', existing.id, batch_id=batch.id,
                   run_id=run.id, attempts=existing.attempts, checksum_ok=True, same_file=True)
            return ReceiveOutcome(existing, replayed=True)
        # 已确认槽位收到乱序/异版本/损坏件：记 stale，已确认版本不倒退。
        stale = _mark_stale(db, batch, run, delivered_version, expected_checksum, actual,
                            payload, actor, '乱序回执：槽位已确认版本，旧/异版本不得倒退', stamp)
        return ReceiveOutcome(stale, replayed=False)

    if existing is not None:
        # staging / mismatch 槽位重发：同槽位只保留一条回执，幂等不重复入库。
        existing.attempts += 1
        existing.actual_checksum = actual
        existing.file_size = len(payload)
        existing.delivered_version = delivered_version
        existing.updated_at = stamp
        if matches:
            existing.state = ReceiptState.STAGED
            existing.note = None
        else:
            existing.state = ReceiptState.MISMATCH
            existing.note = '实收文件指纹与交付指纹不符'
        record(db, actor, 'finance.receipt.replayed', existing.id, batch_id=batch.id, run_id=run.id,
               attempts=existing.attempts, checksum_ok=matches,
               same_file=existing.expected_checksum == expected_checksum and matches)
        return ReceiveOutcome(existing, replayed=True)

    state = ReceiptState.STAGED if matches else ReceiptState.MISMATCH
    receipt = FinanceReceipt(batch_id=batch.id, run_id=run.id, report_name=run.name,
                             delivered_version=delivered_version, expected_checksum=expected_checksum,
                             actual_checksum=actual, file_size=len(payload), state=state, attempts=1,
                             note=None if matches else '实收文件指纹与交付指纹不符',
                             created_at=stamp, updated_at=stamp)
    db.add(receipt)
    db.flush()
    record(db, actor, f'finance.receipt.{state}', receipt.id, batch_id=batch.id, run_id=run.id,
           delivered_version=delivered_version, checksum_ok=matches)
    return ReceiveOutcome(receipt, replayed=False)


def _slots(batch: FinanceBatch, receipts: list[FinanceReceipt]) -> dict:
    tracked = [r for r in receipts if r.state != ReceiptState.STALE]
    received = [r for r in tracked if r.state in (ReceiptState.STAGED, ReceiptState.CONFIRMED)]
    mismatched = [r for r in tracked if r.state == ReceiptState.MISMATCH]
    archived = [r for r in tracked if r.state == ReceiptState.SUPERSEDED]
    stale = [r for r in receipts if r.state == ReceiptState.STALE]
    return {'tracked': tracked, 'received': received, 'mismatched': mismatched,
            'archived': archived, 'stale': stale}


def evaluate_batch(batch: FinanceBatch, receipts: list[FinanceReceipt]) -> dict:
    slots = _slots(batch, receipts)
    ready = (batch.state == BatchState.STAGING
             and len(slots['received']) == batch.expected_slots
             and not slots['mismatched'])
    missing = batch.expected_slots - len(slots['received'])
    return {**slots, 'ready': ready, 'missing': max(0, missing)}


def activate_batch(db: Session, batch: FinanceBatch, actor: str) -> FinanceBatch:
    """整批生效：槽位补齐且无指纹不符才允许；激活同时把旧 active 批次降为 superseded。"""
    receipts = list(db.scalars(select(FinanceReceipt).where(
        FinanceReceipt.batch_id == batch.id).with_for_update()))
    verdict = evaluate_batch(batch, receipts)
    if batch.state not in (BatchState.STAGING, BatchState.FAILED):
        raise ValueError(f'批次当前为 {batch.state}，不能整批生效')
    if verdict['mismatched']:
        reason = '存在指纹不符的回执，请按原文件补齐后再整批生效'
        batch.state = BatchState.FAILED
        batch.error = reason
        record(db, actor, 'finance.batch.blocked', batch.id, mismatch=len(verdict['mismatched']))
        raise BatchActivationError(reason)
    if verdict['missing']:
        reason = f'尚有 {verdict["missing"]} 个报告未补齐，旧版继续生效'
        batch.state = BatchState.FAILED
        batch.error = reason
        record(db, actor, 'finance.batch.blocked', batch.id, missing=verdict['missing'])
        raise BatchActivationError(reason)

    stamp = now()
    previous = db.scalar(select(FinanceBatch).where(
        FinanceBatch.batch_code == batch.batch_code,
        FinanceBatch.state == BatchState.ACTIVE).with_for_update())
    if previous is not None:
        previous.state = BatchState.SUPERSEDED
        previous.superseded_at = stamp
        for old in db.scalars(select(FinanceReceipt).where(
                FinanceReceipt.batch_id == previous.id,
                FinanceReceipt.state == ReceiptState.CONFIRMED)):
            old.state = ReceiptState.SUPERSEDED
        record(db, actor, 'finance.batch.superseded', previous.id, by_batch=batch.id)

    for receipt in verdict['received']:
        receipt.state = ReceiptState.CONFIRMED
        receipt.confirmed_at = stamp
    batch.state = BatchState.ACTIVE
    batch.activated_at = stamp
    batch.error = None
    record(db, actor, 'finance.batch.activated', batch.id, batch_code=batch.batch_code,
           seq=batch.seq, slots=len(verdict['received']))
    return batch


def reopen_batch(db: Session, batch: FinanceBatch, actor: str) -> FinanceBatch:
    """把写一半挂起的批次重新打开以便补齐；已生效/已取代的批次不允许重开。"""
    if batch.state != BatchState.FAILED:
        raise ValueError(f'仅挂起（failed）批次可重开，当前为 {batch.state}')
    batch.state = BatchState.STAGING
    record(db, actor, 'finance.batch.reopened', batch.id)
    return batch


def reconciliation(db: Session, batch_code: str) -> dict:
    """报告交付对账：按实收版本和指纹逐槽核对，单列指纹不符与乱序（stale）回执。"""
    batches = list(db.scalars(select(FinanceBatch).where(
        FinanceBatch.batch_code == batch_code).order_by(FinanceBatch.seq.desc())))
    if not batches:
        return {'batch_code': batch_code, 'batches': [], 'active_seq': None}
    receipts = list(db.scalars(select(FinanceReceipt).join(
        FinanceBatch, FinanceBatch.id == FinanceReceipt.batch_id).where(
        FinanceBatch.batch_code == batch_code).order_by(FinanceReceipt.id.desc())))
    by_batch: dict[int, list[FinanceReceipt]] = {}
    for receipt in receipts:
        by_batch.setdefault(receipt.batch_id, []).append(receipt)
    active = next((b for b in batches if b.state == BatchState.ACTIVE), None)
    result = []
    for batch in batches:
        items = by_batch.get(batch.id, [])
        verdict = evaluate_batch(batch, items)
        result.append({
            'id': batch.id, 'seq': batch.seq, 'state': batch.state,
            'expected_slots': batch.expected_slots, 'error': batch.error,
            'created_at': batch.created_at, 'activated_at': batch.activated_at,
            'received_count': len(verdict['received']),
            'archived_count': len(verdict['archived']),
            'mismatch_count': len(verdict['mismatched']),
            'stale_count': len(verdict['stale']),
            'ready': verdict['ready'],
            'slots': [_slot_view(run_lookup(db, r), r) for r in verdict['tracked']],
            'stale': [_slot_view(run_lookup(db, r), r) for r in verdict['stale']],
        })
    return {'batch_code': batch_code, 'active_seq': active.seq if active else None, 'batches': result}


def run_lookup(db: Session, receipt: FinanceReceipt) -> Run:
    return db.get(Run, receipt.run_id)


def _slot_view(run: Run, receipt: FinanceReceipt) -> dict:
    return {
        'receipt_id': receipt.id, 'run_id': receipt.run_id, 'report_name': receipt.report_name,
        'delivered_version': receipt.delivered_version, 'state': receipt.state,
        'expected_checksum': receipt.expected_checksum, 'actual_checksum': receipt.actual_checksum,
        'checksum_ok': receipt.expected_checksum == receipt.actual_checksum,
        'file_size': receipt.file_size, 'attempts': receipt.attempts, 'note': receipt.note,
        'updated_at': receipt.updated_at,
    }
