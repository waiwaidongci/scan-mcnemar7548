from enum import StrEnum

class Role(StrEnum):
    ADMIN = 'admin'
    ANALYST = 'analyst'
    VIEWER = 'viewer'

class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'

class Review(StrEnum):
    PENDING = 'pending'
    ACCEPTED = 'accepted'
    RETURNED = 'returned'

class BatchState(StrEnum):
    STAGING = 'staging'
    ACTIVE = 'active'
    SUPERSEDED = 'superseded'
    FAILED = 'failed'

class ReceiptState(StrEnum):
    STAGED = 'staged'
    MISMATCH = 'mismatch'
    CONFIRMED = 'confirmed'
    SUPERSEDED = 'superseded'
    STALE = 'stale'
