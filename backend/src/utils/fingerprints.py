import hashlib


def file_fingerprint(payload: bytes) -> str:
    """对完整文件字节计算 SHA-256，作为交付与验收双方对账的完整文件指纹。"""
    return hashlib.sha256(payload).hexdigest()


def short(fingerprint: str, length: int = 12) -> str:
    return f'{fingerprint[:length]}…' if len(fingerprint) > length else fingerprint
