import json
from decimal import Decimal
import pytest
from app import withdrawal_store
from app.errors import DomainError
from app.pipeline import calculate
from app.modules.m8_withdrawal import rules


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_DB', str(tmp_path / 'db.sqlite3'))


@pytest.fixture
def approved(raw):
    result = calculate(raw)
    return {**result, 'decision': '通过', 'revision': 2}


def payments(**kwargs):
    """未点名账户默认未付；按持仓编号指定已付/在途。"""
    return [{'record': record, 'status': status, 'amount': amount, 'voucher': f'V-{record}'}
            for record, (status, amount) in kwargs.items()]


def test_unpaid_closure_has_zero_net_and_no_receivable(approved):
    result = withdrawal_store.submit(approved, 'W-1', [])
    assert result['totals']['冲减_CNY'] == result['totals']['应付_CNY'] == '276.38'
    assert result['totals']['转应收_CNY'] == '0.00'
    assert result['totals']['终止支付_CNY'] == '276.38'
    assert result['can_close'] is True and result['status'] == '可结批'
    assert all(row['处理'] == '未付款终止支付' for row in result['rows'])


def test_partial_paid_keeps_customer_balance_and_flags_receivable(approved):
    result = withdrawal_store.submit(approved, 'W-2', payments(**{'9007199254741001': ('已付', '13.26')}))
    row = next(r for r in result['rows'] if r['持仓记录'] == '9007199254741001')
    assert row['已付_CNY'] == '13.26' and row['未付_CNY'] == '0.00' and row['冲减_CNY'] == '13.26'
    assert row['转应收_CNY'] == '13.26'
    assert result['totals']['转应收_CNY'] == '13.26'
    assert result['totals']['终止支付_CNY'] == '263.12'
    assert any(item['类型'] == '应收追回' and item['金额_CNY'] == '13.26' for item in result['open_items'])
    assert result['can_close'] is True


def test_paid_partial_leaves_rest_unpaid(approved):
    result = withdrawal_store.submit(approved, 'W-3', payments(**{'9007199254741002': ('已付', '5.00')}))
    row = next(r for r in result['rows'] if r['持仓记录'] == '9007199254741002')
    assert (row['已付_CNY'], row['未付_CNY'], row['冲减_CNY']) == ('5.00', '8.39', '13.39')
    assert row['处理'] == '部分已付转应收，余款终止支付'


def test_unknown_payment_blocks_closure(approved):
    result = withdrawal_store.submit(approved, 'W-4', payments(**{'NO-SUCH-ID': ('已付', '1.00')}))
    assert result['can_close'] is False and result['status'] == '不可结批'
    assert any(item['原因码'] == 'UNKNOWN_ACCOUNT' for item in result['exceptions'])


def test_payment_for_conflicted_account_goes_to_recovery(approved):
    # 000003 的行动按冲突隔离，该客户没有本批应付；其已付款不参与冲减。
    result = withdrawal_store.submit(approved, 'W-5', [{'record': '9007199254741003', 'security': '000003',
                                                        'status': '已付', 'amount': '10.00', 'voucher': 'VX'}])
    assert result['totals']['冲减_CNY'] == result['totals']['应付_CNY']
    assert result['outside'][0]['金额_CNY'] == '10.00'
    assert any(item['类型'] == '非本批付款追回' for item in result['open_items'])
    assert result['can_close'] is True


def test_overpay_recovered_not_offset(approved):
    result = withdrawal_store.submit(approved, 'W-6', payments(**{'9007199254741001': ('已付', '20.00')}))
    row = next(r for r in result['rows'] if r['持仓记录'] == '9007199254741001')
    assert row['已付_CNY'] == '20.00' and row['超额_CNY'] == '6.74' and row['冲减_CNY'] == '13.26'
    assert result['totals']['转应收_CNY'] == '20.00'
    assert result['totals']['冲减_CNY'] == '276.38'


def test_transit_blocks_until_confirmed(approved):
    result = withdrawal_store.submit(approved, 'W-7', payments(**{'9007199254741001': ('在途', '13.26')}))
    assert result['can_close'] is False
    assert result['totals']['在途_CNY'] == '13.26' and result['totals']['未付_CNY'] == '263.12'


def test_same_decision_is_idempotent(approved):
    first = withdrawal_store.submit(approved, 'W-8', payments(**{'9007199254741001': ('已付', '13.26')}))
    again = withdrawal_store.submit(approved, 'W-8', payments(**{'9007199254741001': ('已付', '13.26')}))
    assert again['reused'] is True
    assert first['confirmed_sha256'] == again['confirmed_sha256']
    assert len(withdrawal_store.history(approved['manifest']['run_id'])) == 1


def test_same_decision_with_new_content_conflicts(approved):
    withdrawal_store.submit(approved, 'W-9', payments(**{'9007199254741001': ('已付', '13.26')}))
    with pytest.raises(DomainError) as error:
        withdrawal_store.submit(approved, 'W-9', payments(**{'9007199254741001': ('已付', '14.00')}))
    assert error.value.status == 409


def test_second_decision_for_same_batch_rejected(approved):
    withdrawal_store.submit(approved, 'W-10', [])
    with pytest.raises(DomainError) as error:
        withdrawal_store.submit(approved, 'W-11', [])
    assert error.value.code == 'WITHDRAWAL_EXISTS'


def test_late_payment_does_not_rewrite_confirmed_result(approved):
    first = withdrawal_store.submit(approved, 'W-12', [])
    confirmed = first['confirmed_rows']
    later = withdrawal_store.append_event(approved['manifest']['run_id'], 'W-12',
                                          {'record': '9007199254741001', 'status': '已付', 'amount': '13.26', 'voucher': 'LATE'})
    assert later['confirmed_rows'] == confirmed
    assert later['confirmed_totals'] == first['confirmed_totals']
    assert later['totals']['冲减_CNY'] == '276.38'
    row = next(r for r in later['rows'] if r['持仓记录'] == '9007199254741001')
    assert row['已付_CNY'] == '13.26' and row['未付_CNY'] == '0.00'
    assert later['events'][0]['应用结果'] == '未付/在途转已付 13.26'


def test_late_event_is_idempotent(approved):
    withdrawal_store.submit(approved, 'W-13', [])
    body = {'record': '9007199254741001', 'status': '已付', 'amount': '13.26'}
    withdrawal_store.append_event(approved['manifest']['run_id'], 'W-13', body)
    again = withdrawal_store.append_event(approved['manifest']['run_id'], 'W-13', body)
    assert again['event_reused'] is True and len(again['events']) == 1


def test_late_payment_after_closure_preserves_result(approved):
    withdrawal_store.submit(approved, 'W-14', [])
    closed = withdrawal_store.close(approved['manifest']['run_id'], 'W-14', 1)
    assert closed['state'] == '已结批'
    later = withdrawal_store.append_event(approved['manifest']['run_id'], 'W-14',
                                          {'record': '9007199254741001', 'status': '已付', 'amount': '13.26'})
    assert later['totals']['转应收_CNY'] == closed['totals']['转应收_CNY'] == '0.00'
    assert later['outside'][0]['类型'] == '结批后晚到付款'
    assert later['confirmed_totals'] == closed['confirmed_totals']


def test_close_blocked_by_transit_then_unblocked_by_stop_confirmation(approved):
    withdrawal_store.submit(approved, 'W-15', payments(**{'9007199254741001': ('在途', '13.26')}))
    with pytest.raises(DomainError) as error:
        withdrawal_store.close(approved['manifest']['run_id'], 'W-15', 1)
    assert error.value.status == 409
    withdrawal_store.append_event(approved['manifest']['run_id'], 'W-15',
                                  {'record': '9007199254741001', 'status': '未付', 'amount': '13.26'})
    closed = withdrawal_store.close(approved['manifest']['run_id'], 'W-15', 1)
    assert closed['state'] == '已结批' and closed['can_close'] is True


def test_close_is_idempotent(approved):
    withdrawal_store.submit(approved, 'W-16', [])
    first = withdrawal_store.close(approved['manifest']['run_id'], 'W-16', 1)
    again = withdrawal_store.close(approved['manifest']['run_id'], 'W-16', 1)
    assert first['state'] == again['state'] == '已结批' and again['close_reused'] is True
    assert first['confirmed_totals'] == again['confirmed_totals']


def test_close_rejects_stale_revision(approved):
    withdrawal_store.submit(approved, 'W-20', payments(**{'9007199254741001': ('在途', '13.26')}))
    withdrawal_store.append_event(approved['manifest']['run_id'], 'W-20',
                                  {'record': '9007199254741001', 'status': '未付', 'amount': '13.26'})
    with pytest.raises(DomainError) as error:
        withdrawal_store.close(approved['manifest']['run_id'], 'W-20', 99)
    assert error.value.status == 409


def test_only_reviewed_batch_can_be_withdrawn(raw):
    base = {**calculate(raw), 'decision': '待复核'}
    with pytest.raises(DomainError) as error:
        withdrawal_store.submit(base, 'W-17', [])
    assert error.value.status == 422


def test_duplicate_payment_rows_counted_not_double_posted(approved):
    entry = {'record': '9007199254741001', 'status': '已付', 'amount': '13.26', 'voucher': 'DUP'}
    result = withdrawal_store.submit(approved, 'W-18', [entry, dict(entry)])
    assert result['duplicates'] == 1
    row = next(r for r in result['rows'] if r['持仓记录'] == '9007199254741001')
    assert row['已付_CNY'] == '13.26'


def test_paid_and_transit_for_same_account_cannot_exceed_payable(approved):
    result = withdrawal_store.submit(approved, 'W-19', [
        {'record': '9007199254741001', 'status': '已付', 'amount': '10.00'},
        {'record': '9007199254741001', 'status': '在途', 'amount': '10.00'},
    ])
    row = next(r for r in result['rows'] if r['持仓记录'] == '9007199254741001')
    assert row['已付_CNY'] == '10.00' and row['在途_CNY'] == '10.00'
    assert row['超额_CNY'] == '6.74' and row['未付_CNY'] == '0.00'
    assert row['冲减_CNY'] == '13.26'


def test_no_holdings_cannot_be_withdrawn():
    with pytest.raises(DomainError):
        rules.build({'decision': '通过', 'holdings': [], 'positions': [], 'manifest': {'run_id': 'x'}}, 'W', [])


def test_api_requires_reviewed_batch_and_keeps_batch_immutable(client, raw):
    run = client.post('/api/run', json={}).get_json()
    ident = run['manifest']['run_id']
    body = {'run_id': ident, 'withdrawal_id': 'API-1', 'payments': [
        {'record': '9007199254741001', 'status': '已付', 'amount': '13.26', 'voucher': 'V1'}]}
    assert client.post('/api/withdrawal', json=body).status_code == 422
    assert client.post('/api/review', json={'id': ident, 'revision': 1, 'decision': '通过'}).status_code == 200
    first = client.post('/api/withdrawal', json=body).get_json()
    assert first['totals']['冲减_CNY'] == '276.38' and first['totals']['转应收_CNY'] == '13.26'
    page = client.get('/work/8?run_id=' + ident).data.decode()
    assert '撤销冲减' in page and '13.26' in page
    batch_evidence = client.get('/api/evidence.csv?run_id=' + ident).data.decode('utf-8-sig')
    assert '已付' not in batch_evidence
    evidence = client.get('/api/withdrawal/evidence.csv?run_id=' + ident + '&withdrawal_id=API-1').data.decode('utf-8-sig')
    assert '已付' in evidence and '13.26' in evidence and '276.38' in evidence
    assert client.get('/api/runs/' + ident).get_json()['decision'] == '通过'


def test_api_same_decision_twice_does_not_double_post(client):
    run = client.post('/api/run', json={}).get_json()
    ident = run['manifest']['run_id']
    client.post('/api/review', json={'id': ident, 'revision': 1, 'decision': '通过'})
    body = {'run_id': ident, 'withdrawal_id': 'API-2', 'payments': []}
    first = client.post('/api/withdrawal', json=body).get_json()
    again = client.post('/api/withdrawal', json=body).get_json()
    assert first['reused'] is False and again['reused'] is True
    assert again['totals'] == first['totals']
    assert len(client.get('/api/withdrawals?run_id=' + ident).get_json()) == 1


def test_api_late_payment_appends_event_without_changing_confirmed(client):
    run = client.post('/api/run', json={}).get_json()
    ident = run['manifest']['run_id']
    client.post('/api/review', json={'id': ident, 'revision': 1, 'decision': '通过'})
    body = {'run_id': ident, 'withdrawal_id': 'API-3', 'payments': []}
    client.post('/api/withdrawal', json=body)
    late = client.post('/api/withdrawal/events', json={**body, 'event': {
        'record': '9007199254741001', 'status': '已付', 'amount': '13.26', 'voucher': 'LATE'}}).get_json()
    assert late['confirmed_totals']['转应收_CNY'] == '0.00'
    assert late['totals']['转应收_CNY'] == '13.26' and late['totals']['冲减_CNY'] == '276.38'


def test_api_close_uses_revision(client):
    run = client.post('/api/run', json={}).get_json()
    ident = run['manifest']['run_id']
    client.post('/api/review', json={'id': ident, 'revision': 1, 'decision': '通过'})
    client.post('/api/withdrawal', json={'run_id': ident, 'withdrawal_id': 'API-4'})
    assert client.post('/api/withdrawal/close', json={'run_id': ident, 'withdrawal_id': 'API-4', 'revision': 1}).get_json()['state'] == '已结批'
    assert client.post('/api/withdrawal/close', json={'run_id': ident, 'withdrawal_id': 'API-4', 'revision': 1}).get_json()['close_reused'] is True


def test_api_rejects_non_object_json(client):
    assert client.post('/api/withdrawal', data='[]', content_type='application/json').status_code == 400
    assert client.post('/api/withdrawal/events', data='null', content_type='application/json').status_code == 400
    assert client.post('/api/withdrawal/close', data='[]', content_type='application/json').status_code == 400


def test_payments_default_to_reviewed_channel_cash(approved):
    # 已复核记录中渠道报送现金即已付证据；缺省台账据此派生。
    result = withdrawal_store.submit(approved, 'W-21', None)
    assert result['totals']['已付_CNY'] == '276.38'
    assert result['totals']['转应收_CNY'] == '276.38'
    assert result['totals']['冲减_CNY'] == '276.38'
    assert all(row['已付_CNY'] == row['应付_CNY'] for row in result['rows'])
    # 000003 行动冲突被隔离，其客户报送的 13.52 不属本批应付，单列追回。
    assert result['outside'][0]['金额_CNY'] == '13.52'
    assert result['can_close'] is True


def test_explicit_empty_ledger_means_nothing_paid(approved):
    explicit = withdrawal_store.submit(approved, 'W-22', [])
    assert explicit['totals']['转应收_CNY'] == '0.00'
    assert explicit['totals']['终止支付_CNY'] == '276.38'
    assert explicit['outside'] == []


def test_same_decision_derived_and_explicit_conflict(approved):
    withdrawal_store.submit(approved, 'W-24', None)
    with pytest.raises(DomainError) as error:
        withdrawal_store.submit(approved, 'W-24', [])
    assert error.value.status == 409
