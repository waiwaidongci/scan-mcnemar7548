import pytest

from src.constants.enums import BatchState, ReceiptState
from src.models.jobs import Run
from src.services import finance
from src.utils.fingerprints import file_fingerprint


def make_runs(db, count=3):
    runs = []
    for index in range(1, count + 1):
        job = Run(name=f'财务交付报告 {index}', dataset_id=1, params={}, author='seed',
                  status='complete', export_file=None)
        db.add(job)
        db.flush()
        runs.append(job)
    return runs


def packet_for(run, seq, tamper=False, version=''):
    payload = f'canonical-report-{run.id}'.encode()
    expected = file_fingerprint(payload)
    if tamper:
        payload = payload + b'#tampered'
    return payload, expected, version or f'v{seq}.r{run.id}'


def receive(db, batch, run, seq, tamper=False, version=''):
    payload, expected, ver = packet_for(run, seq, tamper, version)
    return finance.receive_report(db, batch, run, ver, payload, expected, 'admin')


def test_batch_whole_version_swap_atomic(db):
    r1, r2, r3 = make_runs(db)
    b1 = finance.open_batch(db, 'FIN-X', 3, 'admin')
    for run in (r1, r2, r3):
        receive(db, b1, run, b1.seq)
    finance.activate_batch(db, b1, 'admin')
    assert b1.state == BatchState.ACTIVE
    confirmed = [r.state for r in db.query(finance.FinanceReceipt).all()]
    assert confirmed == [ReceiptState.CONFIRMED] * 3

    b2 = finance.open_batch(db, 'FIN-X', 3, 'admin')
    assert b2.seq == b1.seq + 1
    for run in (r1, r2, r3):
        receive(db, b2, run, b2.seq)
    finance.activate_batch(db, b2, 'admin')
    db.refresh(b1)
    assert b2.state == BatchState.ACTIVE and b1.state == BatchState.SUPERSEDED
    old = db.query(finance.FinanceReceipt).filter_by(batch_id=b1.id).all()
    assert {r.state for r in old} == {ReceiptState.SUPERSEDED}


def test_idempotent_retry_does_not_duplicate(db):
    r1, = make_runs(db, 1)
    b1 = finance.open_batch(db, 'FIN-Y', 1, 'admin')
    first = receive(db, b1, r1, b1.seq).receipt
    finance.activate_batch(db, b1, 'admin')
    # 已入库却没回执：沿用原文件重试。
    again = receive(db, b1, r1, b1.seq)
    assert again.replayed is True
    assert again.receipt.id == first.id
    assert again.receipt.attempts == 2
    assert db.query(finance.FinanceReceipt).count() == 1


def test_checksum_mismatch_is_separated_and_blocks_activation(db):
    r1, r2 = make_runs(db, 2)
    b1 = finance.open_batch(db, 'FIN-Z', 2, 'admin')
    bad = receive(db, b1, r1, b1.seq, tamper=True).receipt
    good = receive(db, b1, r2, b1.seq).receipt
    assert bad.state == ReceiptState.MISMATCH and good.state == ReceiptState.STAGED
    with pytest.raises(finance.BatchActivationError):
        finance.activate_batch(db, b1, 'admin')
    assert b1.state == BatchState.FAILED and b1.error
    verdict = finance.evaluate_batch(b1, db.query(finance.FinanceReceipt).all())
    assert [r.report_name for r in verdict['mismatched']] == [r1.name]


def test_half_written_keeps_old_version_then_completes(db):
    r1, r2, r3 = make_runs(db, 3)
    b1 = finance.open_batch(db, 'FIN-W', 3, 'admin')
    for run in (r1, r2, r3):
        receive(db, b1, run, b1.seq)
    finance.activate_batch(db, b1, 'admin')

    b2 = finance.open_batch(db, 'FIN-W', 3, 'admin')
    receive(db, b2, r1, b2.seq)
    with pytest.raises(finance.BatchActivationError):
        finance.activate_batch(db, b2, 'admin')
    db.refresh(b1)
    # 新版没写完：旧版仍是 active，新版挂起。
    assert b1.state == BatchState.ACTIVE and b2.state == BatchState.FAILED
    # 重开挂起批次、补齐后再整批生效。
    finance.reopen_batch(db, b2, 'admin')
    assert b2.state == BatchState.STAGING
    receive(db, b2, r2, b2.seq)
    receive(db, b2, r3, b2.seq)
    finance.activate_batch(db, b2, 'admin')
    db.refresh(b1)
    assert b2.state == BatchState.ACTIVE and b1.state == BatchState.SUPERSEDED


def test_late_old_report_cannot_overwrite_new_version(db):
    r1, r2 = make_runs(db, 2)
    b1 = finance.open_batch(db, 'FIN-L', 2, 'admin')
    for run in (r1, r2):
        receive(db, b1, run, b1.seq)
    finance.activate_batch(db, b1, 'admin')

    b2 = finance.open_batch(db, 'FIN-L', 2, 'admin')
    for run in (r1, r2):
        receive(db, b2, run, b2.seq)
    finance.activate_batch(db, b2, 'admin')

    # 向已 superseded 的 v1 迟到投递旧报告。
    late = receive(db, b1, r1, b1.seq, version='v1.r1')
    assert late.receipt.state == ReceiptState.STALE
    # 向当前 active 已确认槽位塞旧版本（乱序）。
    disorder = receive(db, b2, r2, b2.seq, version='v1.r2')
    assert disorder.receipt.state == ReceiptState.STALE

    active_confirmed = db.query(finance.FinanceReceipt).filter_by(
        batch_id=b2.id, state=ReceiptState.CONFIRMED).count()
    assert active_confirmed == 2
    recon = finance.reconciliation(db, 'FIN-L')
    assert recon['active_seq'] == b2.seq
    latest = recon['batches'][0]
    assert latest['stale_count'] == 1
    assert latest['slots'][0]['checksum_ok'] is True


def test_mismatch_repair_with_correct_file_then_activates(db):
    r1, = make_runs(db, 1)
    b1 = finance.open_batch(db, 'FIN-R', 1, 'admin')
    bad = receive(db, b1, r1, b1.seq, tamper=True)
    assert bad.receipt.state == ReceiptState.MISMATCH
    fixed = receive(db, b1, r1, b1.seq)  # 重发正确文件
    assert fixed.receipt.state == ReceiptState.STAGED
    assert fixed.receipt.attempts == 2
    finance.activate_batch(db, b1, 'admin')
    assert b1.state == BatchState.ACTIVE
