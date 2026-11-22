"""交付报文：财务侧从运行导出文件读取完整字节、版本与文件指纹。

交付方按「交付版本 + 完整文件指纹」随报文提供；财务端不信任随报指纹，
而是对实收字节重新计算 SHA-256 后再核对，从而识别传输损坏与篡改。
"""

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.config.settings import ARTIFACTS
from src.constants.enums import Status
from src.models.jobs import Run
from src.utils.fingerprints import file_fingerprint


@dataclass
class Packet:
    run: Run
    delivered_version: str
    payload: bytes
    expected_checksum: str


def canonical_version(batch_seq: int, run_id: int) -> str:
    return f'v{batch_seq}.r{run_id}'


def load_packet(db: Session, batch_seq: int, run_id: int, tamper: bool,
                version_override: str) -> Packet:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, '找不到该运行记录')
    if run.status != Status.COMPLETE or not run.export_file:
        raise HTTPException(409, '该运行尚未生成可交付的报告')
    path = ARTIFACTS / run.export_file
    if not path.is_file():
        raise HTTPException(410, '报告文件已不可用，请重新运行')
    payload = path.read_bytes()
    expected = file_fingerprint(payload)
    if tamper:
        # 演示「实收字节 ≠ 交付文件」：财务端重算指纹必然对不上。
        payload = payload + b'\n# finance-demo: tampered byte\n'
    return Packet(run=run,
                  delivered_version=version_override.strip() or canonical_version(batch_seq, run_id),
                  payload=payload, expected_checksum=expected)
