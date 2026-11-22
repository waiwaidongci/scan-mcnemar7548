from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.finance import FinanceBatch, FinanceReceipt
from src.models.identity import User
from src.schemas.requests import FinanceBatchRequest, FinanceReceiveRequest
from src.services import finance
from src.services.finance_demo import run_demo
from src.services.finance_packets import load_packet
from src.utils import views

router = APIRouter(prefix='/finance')


def require_batch(db: Session, batch_id: int) -> FinanceBatch:
    batch = db.get(FinanceBatch, batch_id, with_for_update=True)
    if not batch:
        raise HTTPException(404, '找不到该财务换版批次')
    return batch


@router.get('/batches')
def list_batches(db: Session = Depends(connection), actor: User = Depends(current)):
    items = db.scalars(select(FinanceBatch).order_by(FinanceBatch.id.desc()).limit(100))
    return [views.finance_batch(item) for item in items]


@router.post('/batches', status_code=201)
def open_batch(body: FinanceBatchRequest, db: Session = Depends(connection),
               actor: User = Depends(allow(Role.ADMIN))):
    batch = finance.open_batch(db, body.batch_code.strip(), body.expected_slots, actor.username)
    db.commit()
    db.refresh(batch)
    return views.finance_batch(batch)


@router.post('/batches/{batch_id}/receive', status_code=201)
def receive(batch_id: int, body: FinanceReceiveRequest, db: Session = Depends(connection),
            actor: User = Depends(allow(Role.ADMIN))):
    batch = require_batch(db, batch_id)
    packet = load_packet(db, batch.seq, body.run_id, body.tamper, body.delivered_version)
    outcome = finance.receive_report(db, batch, packet.run, packet.delivered_version,
                                     packet.payload, packet.expected_checksum, actor.username)
    db.commit()
    db.refresh(outcome.receipt)
    return {**views.finance_receipt(outcome.receipt), 'replayed': outcome.replayed}


@router.post('/batches/{batch_id}/activate')
def activate(batch_id: int, db: Session = Depends(connection),
             actor: User = Depends(allow(Role.ADMIN))):
    batch = require_batch(db, batch_id)
    try:
        finance.activate_batch(db, batch, actor.username)
    except finance.BatchActivationError as error:
        # 未达标是业务结果而非系统错误：批次置为 failed 并保留旧版，状态需要落库。
        db.commit()
        db.refresh(batch)
        raise HTTPException(409, str(error)) from None
    except ValueError as error:
        db.rollback()
        raise HTTPException(409, str(error)) from None
    db.commit()
    db.refresh(batch)
    return views.finance_batch(batch)


@router.post('/batches/{batch_id}/reopen')
def reopen(batch_id: int, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN))):
    batch = require_batch(db, batch_id)
    try:
        finance.reopen_batch(db, batch, actor.username)
    except ValueError as error:
        db.rollback()
        raise HTTPException(409, str(error)) from None
    db.commit()
    db.refresh(batch)
    return views.finance_batch(batch)


@router.get('/batches/{batch_id}')
def batch_detail(batch_id: int, db: Session = Depends(connection),
                 actor: User = Depends(current)):
    batch = db.get(FinanceBatch, batch_id)
    if not batch:
        raise HTTPException(404, '找不到该财务换版批次')
    receipts = db.scalars(select(FinanceReceipt).where(
        FinanceReceipt.batch_id == batch_id).order_by(FinanceReceipt.id))
    return {**views.finance_batch(batch),
            'receipts': [views.finance_receipt(item) for item in receipts]}


@router.get('/reconciliation')
def reconciliation(batch_code: str, db: Session = Depends(connection),
                   actor: User = Depends(current)):
    return finance.reconciliation(db, batch_code.strip())


@router.post('/demo/run')
def demo_run(db: Session = Depends(connection), actor: User = Depends(allow(Role.ADMIN))):
    """一键重置并演完整批换版：幂等重试、指纹不符、半成品保护、乱序防护。"""
    result = run_demo(db, actor.username)
    db.commit()
    return result
