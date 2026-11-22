from datetime import datetime
from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.constants.enums import BatchState, ReceiptState
from src.utils.time import now


class FinanceBatch(Base):
    """财务按批次整批换版的入库单。seq 单调递增，active 批次任一时刻至多一个。"""

    __tablename__ = 'finance_batches'
    __table_args__ = (
        Index('uq_finance_batch_version', 'batch_code', 'seq', unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_code: Mapped[str] = mapped_column(String(120), index=True)
    seq: Mapped[int]
    state: Mapped[str] = mapped_column(String(20), default=BatchState.STAGING, index=True)
    expected_slots: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(String(500))
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    activated_at: Mapped[datetime | None]
    superseded_at: Mapped[datetime | None]


class FinanceReceipt(Base):
    """单个报告槽位的财务回执：按交付版本和完整文件指纹验收入库。"""

    __tablename__ = 'finance_receipts'
    __table_args__ = (
        # 在用槽位（非 stale）每批次每运行至多一条；stale 留痕行可多条，不参与唯一约束。
        Index('uq_finance_live_receipt', 'batch_id', 'run_id', unique=True,
              sqlite_where=text("state <> 'stale'"),
              postgresql_where=text("state <> 'stale'")),
        CheckConstraint('attempts >= 1', name='ck_finance_receipt_attempts'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('finance_batches.id'), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'))
    report_name: Mapped[str] = mapped_column(String(160))
    delivered_version: Mapped[str] = mapped_column(String(40))
    expected_checksum: Mapped[str] = mapped_column(String(64))
    actual_checksum: Mapped[str] = mapped_column(String(64))
    file_size: Mapped[int]
    state: Mapped[str] = mapped_column(String(20), default=ReceiptState.STAGED, index=True)
    attempts: Mapped[int] = mapped_column(default=1)
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)
    confirmed_at: Mapped[datetime | None]
