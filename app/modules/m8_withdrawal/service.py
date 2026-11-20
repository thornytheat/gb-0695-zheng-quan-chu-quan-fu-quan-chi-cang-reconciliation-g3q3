from app import withdrawal_store
from app.errors import DomainError


def execute(base, withdrawal_id, payments, reason=''):
    if base is None:
        raise DomainError('NOT_FOUND', '基准批次不存在', 404)
    return withdrawal_store.submit(base, withdrawal_id, payments, reason)
