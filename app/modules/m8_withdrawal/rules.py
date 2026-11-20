"""撤销冲减：对已复核批次做追加式冲减，不回写原批次与客户已收凭证。

处理原则：
- 未付部分：冲回应付并终止支付，不再出款。
- 已付部分：不回写客户余额与历史凭证，原分录保留，改挂对客户应收，结批后转入追回。
- 在途部分：先发止付指令，确认前本批不可结束。
- 每户冲减额恒等于已复核应付额，整批冲减合计等于整批应付合计，撤销后本批净支出为零。
- 同一撤销决定（基准批次 + 撤销决定编号）只冲减一次；内容不一致按版本冲突处理。
- 晚到付款/止付确认只追加事件；已确认快照、每户冲减额与已结批结果不被改写。
"""
from copy import deepcopy
from decimal import Decimal
import hashlib, json
from app.errors import DomainError
from app.numeric import fmt, number

CENT = Decimal('0.01')
PAID = '已付'
TRANSIT = '在途'
UNPAID = '未付'
STATUSES = (PAID, TRANSIT, UNPAID)
BLOCKING = {'MISSING_RECORD', 'UNKNOWN_ACCOUNT', 'AMBIGUOUS_ACCOUNT', 'INVALID_AMOUNT', 'INVALID_STATUS', 'INVALID_PAYMENT'}


def _canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(_canon(value).encode('utf-8')).hexdigest()


def _money(value):
    return fmt(Decimal(value), '0.01')


def _dec(value):
    return Decimal(str(value)).quantize(CENT)


def _exception(record, security, code, message, value):
    return {'持仓记录': record, '证券': security, '原因码': code, '原因': message, '原值': str(value)}


def _amount(entry):
    """付款金额缺省按 0 处理；非法值抛 DomainError。"""
    raw = entry.get('amount')
    if raw is None or str(raw).strip() == '':
        return Decimal(0)
    return number(raw)


def _voucher_text(vouchers):
    if not vouchers:
        return ''
    return '；'.join(f"{item['状态']} {item['金额_CNY']}" + (f"（{item['凭证']}）" if item['凭证'] else '') for item in vouchers)


def _describe(row):
    """给每户生成人类可读的后续处理说明，金额全部来自已确认口径。"""
    paid, transit, unpaid, excess = (_dec(row['已付_CNY']), _dec(row['在途_CNY']),
                                     _dec(row['未付_CNY']), _dec(row['超额_CNY']))
    if transit > 0 and paid > 0:
        row['处理'] = '部分已付转应收，余款止付在途'
    elif transit > 0:
        row['处理'] = '全款在途，止付确认中'
    elif paid > 0 and unpaid > 0:
        row['处理'] = '部分已付转应收，余款终止支付'
    elif paid > 0:
        row['处理'] = '已付款转应收追回'
    else:
        row['处理'] = '未付款终止支付'
    notes = []
    if paid > 0:
        notes.append('已付部分不改写客户余额与历史凭证，按同额登记对客户应收，结批后转追回')
    if unpaid > 0:
        notes.append('未付部分停止支付，不再出款')
    if transit > 0:
        notes.append('在途部分已发止付指令，须取得止付或到账确认后才能结批')
    if excess > 0:
        notes.append('付款超过应付，超出部分另行追回')
    if not paid and not transit:
        notes.append('无付款到账记录，按未付确认')
    row['说明'] = '；'.join(notes)


def _evaluate(rows, exceptions, outside, state='已登记'):
    keys = ('应付', '已付', '在途', '未付', '超额', '冲减', '转应收', '终止支付')
    sums = {key: Decimal(0) for key in keys}
    for row in rows:
        for field in keys:
            sums[field] += _dec(row[field + '_CNY'])
    outside_paid = sum((_dec(item['金额_CNY']) for item in outside), Decimal(0))
    checks = [
        {'检查': '整批冲减与应付一致', '结果': '通过' if sums['冲减'] == sums['应付'] else '不通过',
         '金额_CNY': _money(sums['冲减'])},
        {'检查': '已付+在途+未付=应付+超额',
         '结果': '通过' if sums['已付'] + sums['在途'] + sums['未付'] == sums['应付'] + sums['超额'] else '不通过',
         '金额_CNY': _money(sums['已付'] + sums['在途'] + sums['未付'])},
        {'检查': '撤销后本批净支出归零', '结果': '通过' if sums['应付'] - sums['冲减'] == 0 else '不通过',
         '金额_CNY': _money(sums['应付'] - sums['冲减'])},
        {'检查': '支出分类完备（应收+在途+终止=应付+超额）',
         '结果': '通过' if sums['转应收'] + sums['在途'] + sums['终止支付'] == sums['应付'] + sums['超额'] else '不通过',
         '金额_CNY': _money(sums['转应收'] + sums['在途'] + sums['终止支付'])},
        {'检查': '每户仅冲减一次', '结果': '通过' if len({(row['持仓记录'], row['证券']) for row in rows}) == len(rows) else '不通过',
         '金额_CNY': _money(sums['冲减'])},
    ]
    blockers = []
    transit_rows = [row for row in rows if _dec(row['在途_CNY']) > 0]
    if transit_rows:
        blockers.append(f'{len(transit_rows)} 户付款仍在途，须先取得止付或到账确认')
    for item in exceptions:
        if item['原因码'] in BLOCKING:
            blockers.append(f"付款记录 {item['持仓记录'] or '—'} 无法解释：{item['原因']}")
    open_items = []
    if sums['转应收'] > 0:
        open_items.append({'类型': '应收追回', '金额_CNY': _money(sums['转应收']),
                           '说明': '已付款以同额应收挂账，结批后转追回，不回写客户余额与历史凭证'})
    for item in outside:
        open_items.append({'类型': item.get('类型', '非本批付款追回'), '金额_CNY': item['金额_CNY'],
                           '说明': f"{item['持仓记录']} {item['原因']}"})
    for item in exceptions:
        if item['原因码'] in ('OVERPAY', 'EVENT_EXCESS'):
            open_items.append({'类型': '超额付款追回', '金额_CNY': item['原值'],
                               '说明': f"{item['持仓记录']} {item['原因']}"})
    counts = {
        '已付户': sum(1 for row in rows if _dec(row['已付_CNY']) > 0),
        '在途户': len(transit_rows),
        '未付户': sum(1 for row in rows if _dec(row['已付_CNY']) == 0 and _dec(row['在途_CNY']) == 0),
        '混合户': sum(1 for row in rows if _dec(row['已付_CNY']) > 0 and _dec(row['未付_CNY']) > 0),
        '非本批付款': len(outside),
    }
    can_close = not blockers
    status = '已结批' if state == '已结批' else ('可结批' if can_close else '不可结批')
    return {
        'totals': {key + '_CNY': _money(value) for key, value in sums.items()},
        'outside': outside,
        'cash_out_CNY': _money(sums['已付'] + sums['在途'] + outside_paid),
        'checks': checks,
        'exceptions': exceptions,
        'blockers': blockers,
        'open_items': open_items,
        'counts': counts,
        'can_close': can_close,
        'status': status,
    }


def derive_payments(base):
    """从已复核记录派生付款台账：渠道报送现金即付款证据。

    M6 现金对账中的 reported_cash 是渠道报送的现金金额；本批没有单独的
    付款流水时以此为决策时点的已付证据。报送为缺失或零的持仓视为未付。
    """
    derived = []
    for position in base.get('positions') or []:
        try:
            amount = number(position.get('reported_cash'), nullable=True)
        except DomainError:
            continue
        if amount is None or amount <= 0:
            continue
        derived.append({'record': str(position['record']), 'security': str(position['canonical']),
                        'status': PAID, 'amount': _money(amount), 'voucher': '渠道报送',
                        'paid_at': str(position.get('event_date') or '')})
    return derived


def build(base, withdrawal_id, payments, reason=''):
    """根据已复核批次与决策时点的付款台账，生成一次性冲减快照。

    payments 缺省（None）时按已复核记录中的渠道报送现金派生；显式传空数组
    表示确认无付款到账，二者结果不同。
    """
    if not isinstance(base, dict) or base.get('decision') != '通过':
        raise DomainError('REVIEW', '仅已通过复核的批次可以撤销', 422)
    holdings = base.get('holdings') or []
    if not holdings:
        raise DomainError('EMPTY', '已复核批次没有分红明细', 422)
    if not isinstance(withdrawal_id, str) or not withdrawal_id.strip():
        raise DomainError('WITHDRAWAL', '撤销决定编号不能为空', 422)
    if payments is None:
        payments = derive_payments(base)
    if not isinstance(payments, list):
        raise DomainError('PAYMENT', '付款记录必须为数组', 422)

    holding_index = {}
    for holding in holdings:
        holding_index.setdefault(str(holding['持仓记录']), []).append(holding)
    position_index = {}
    for position in base.get('positions') or []:
        position_index.setdefault(str(position['record']), []).append(position)

    grouped, outside, exceptions = {}, [], []
    duplicates = accepted = 0
    seen = set()
    for entry in payments:
        if not isinstance(entry, dict):
            exceptions.append(_exception('', '', 'INVALID_PAYMENT', '付款记录必须为对象', _canon(entry)))
            continue
        record = str(entry.get('record') or '').strip()
        security = str(entry.get('security') or '').strip()
        status = str(entry.get('status') or '').strip()
        signature = _canon({key: str(entry.get(key, '')) for key in ('record', 'security', 'amount', 'status', 'paid_at', 'voucher')})
        if signature in seen:
            duplicates += 1
            continue
        seen.add(signature)
        if not record:
            exceptions.append(_exception('', security, 'MISSING_RECORD', '付款记录缺少持仓编号', signature))
            continue
        candidates = holding_index.get(record)
        if not candidates:
            # 账户存在但本批没有应付（如行动冲突被隔离），付款不参与冲减，单独追回。
            positions = [item for item in position_index.get(record, []) if not security or str(item['canonical']) == security]
            if positions:
                try:
                    amount = _amount(entry)
                except DomainError:
                    exceptions.append(_exception(record, security, 'INVALID_AMOUNT', '付款金额无效', str(entry.get('amount'))))
                    continue
                if status not in (PAID, TRANSIT) or amount <= 0:
                    exceptions.append(_exception(record, security, 'INVALID_AMOUNT', '非本批付款须为已付或在途的正金额', status))
                    continue
                outside.append({'持仓记录': record, '证券': security or str(positions[0]['canonical']), '状态': status,
                                '金额_CNY': _money(amount.quantize(CENT)), '凭证': str(entry.get('voucher') or ''),
                                '日期': str(entry.get('paid_at') or ''),
                                '类型': '非本批付款追回', '原因': '在本批持仓中但无本次应付，付款不随撤销冲减，须单独追回'})
                accepted += 1
                continue
            exceptions.append(_exception(record, security, 'UNKNOWN_ACCOUNT', '付款记录不属于本批已复核持仓', signature))
            continue
        if security:
            matched = [holding for holding in candidates if str(holding['证券']) == security]
            if not matched:
                exceptions.append(_exception(record, security, 'UNKNOWN_ACCOUNT', '付款记录的证券不在本批持仓内', signature))
                continue
            holding = matched[0]
        elif len(candidates) > 1:
            exceptions.append(_exception(record, '', 'AMBIGUOUS_ACCOUNT', '同一持仓编号对应多只证券，须注明证券', signature))
            continue
        else:
            holding = candidates[0]
        if status not in STATUSES:
            exceptions.append(_exception(record, holding['证券'], 'INVALID_STATUS', '付款状态必须为已付、在途或未付', status))
            continue
        try:
            amount = _amount(entry)
        except DomainError:
            exceptions.append(_exception(record, holding['证券'], 'INVALID_AMOUNT', '付款金额无效', str(entry.get('amount'))))
            continue
        if amount < 0:
            exceptions.append(_exception(record, holding['证券'], 'INVALID_AMOUNT', '付款金额不能为负', _money(amount)))
            continue
        amount = amount.quantize(CENT)
        if status == UNPAID and amount != 0:
            exceptions.append(_exception(record, holding['证券'], 'INVALID_AMOUNT', '未付记录不应携带金额', _money(amount)))
            continue
        group = grouped.setdefault((record, str(holding['证券'])), {'paid': Decimal(0), 'transit': Decimal(0), 'vouchers': []})
        if status == PAID:
            group['paid'] += amount
        elif status == TRANSIT:
            group['transit'] += amount
        if status in (PAID, TRANSIT):
            group['vouchers'].append({'状态': status, '金额_CNY': _money(amount),
                                      '凭证': str(entry.get('voucher') or ''), '日期': str(entry.get('paid_at') or '')})
        accepted += 1

    rows = []
    for holding in holdings:
        record, security = str(holding['持仓记录']), str(holding['证券'])
        group = grouped.get((record, security), {'paid': Decimal(0), 'transit': Decimal(0), 'vouchers': []})
        payable, paid, transit = _dec(holding['分红_CNY']), group['paid'], group['transit']
        excess = max(Decimal(0), paid + transit - payable)
        if excess > 0:
            exceptions.append(_exception(record, security, 'OVERPAY', '已付与在途合计超过应付，超出部分须另行追回', _money(excess)))
        unpaid = payable - (paid + transit - excess)
        if unpaid < 0:
            unpaid = Decimal(0)
        row = {
            '持仓记录': record, '证券': security,
            '应付_CNY': _money(payable), '已付_CNY': _money(paid), '在途_CNY': _money(transit),
            '未付_CNY': _money(unpaid), '超额_CNY': _money(excess), '冲减_CNY': _money(payable),
            '转应收_CNY': _money(paid), '终止支付_CNY': _money(unpaid),
            '付款凭证': _voucher_text(group['vouchers']),
            '凭证明细': group['vouchers'],
        }
        _describe(row)
        rows.append(row)

    result = {'withdrawal_id': withdrawal_id.strip(), 'reason': str(reason or ''), 'rows': rows,
              'outside': outside, 'duplicates': duplicates, 'payment_records': accepted}
    result.update(_evaluate(rows, exceptions, outside, '已登记'))
    return result


def view(snapshot, events=None, state='已登记', revision=1, key=None, reused=False, closed=None):
    """由已确认快照与追加事件派生当前视图；confirmed_* 字段保持决策时点原样。

    closed 为结批时冻结的派生视图；结批后的事件只作追回登记，不改写冻结行。
    """
    if state == '已结批' and closed:
        rows = deepcopy(closed['rows'])
        outside = deepcopy(closed['outside'])
        exceptions = deepcopy(closed.get('exceptions', []))
        applied = list(closed.get('events', []))
        for event in (events or [])[closed.get('through', 0):]:
            applied.append(_apply_event(rows, outside, exceptions, event, state))
    else:
        rows = deepcopy(snapshot['rows'])
        outside = deepcopy(snapshot.get('outside', []))
        exceptions = deepcopy(snapshot.get('exceptions', []))
        applied = [_apply_event(rows, outside, exceptions, event, state) for event in (events or [])]
    result = {
        **snapshot,
        'withdrawal_key': key or snapshot.get('withdrawal_key'),
        'state': state, 'revision': revision, 'reused': reused,
        'rows': rows, 'outside': outside, 'events': applied,
        'confirmed_rows': deepcopy(snapshot['rows']),
        'confirmed_totals': deepcopy(snapshot.get('totals')),
        'confirmed_exceptions': deepcopy(snapshot.get('exceptions', [])),
        'confirmed_can_close': snapshot.get('can_close'),
        'confirmed_sha256': digest(snapshot['rows']),
    }
    result.update(_evaluate(rows, exceptions, outside, state))
    result['derived_sha256'] = digest(rows)
    return result


def freeze(current, events):
    """结批时冻结派生视图，同时记录已应用事件数。"""
    return {'rows': deepcopy(current['rows']), 'outside': deepcopy(current['outside']),
            'exceptions': deepcopy(current['exceptions']), 'events': deepcopy(current['events']),
            'through': len(events or [])}


def _apply_event(rows, outside, exceptions, event, state):
    record = str(event.get('持仓记录') or '')
    security = str(event.get('证券') or '')
    status = str(event.get('状态') or '')
    amount = _dec(event.get('金额_CNY') or 0)
    target = next((row for row in rows if row['持仓记录'] == record and (not security or row['证券'] == security)), None)
    if target is None:
        for item in outside:
            if item['持仓记录'] == record and (not security or item['证券'] == security):
                item['状态'] = status
                return {**event, '应用结果': f'非本批付款更新为{status}，仍单独追回'}
        exceptions.append(_exception(record, security, 'EVENT_UNKNOWN', '晚到事件指向已确认清单之外的持仓', _money(amount)))
        return {**event, '应用结果': '未应用'}
    if state == '已结批':
        # 结批后到账只登记，不改写已确认结果，另行追回。
        outside.append({'持仓记录': record, '证券': target['证券'], '状态': status, '金额_CNY': _money(amount),
                        '凭证': event.get('凭证', ''), '日期': event.get('付款日期', ''),
                        '类型': '结批后晚到付款', '原因': '已结批后到账，已确认结果不变，须按原凭证另行追回'})
        return {**event, '应用结果': '结批后晚到，已确认结果不变，转追回'}
    paid, transit, unpaid, excess = (_dec(target['已付_CNY']), _dec(target['在途_CNY']),
                                     _dec(target['未付_CNY']), _dec(target['超额_CNY']))
    if status == PAID:
        take = min(amount, transit + unpaid)
        from_transit = min(take, transit)
        transit -= from_transit
        unpaid -= take - from_transit
        paid += take
        extra = amount - take
        if extra > 0:
            excess += extra
            exceptions.append(_exception(record, target['证券'], 'EVENT_EXCESS', '晚到付款超过未付与在途合计', _money(extra)))
        change = f'未付/在途转已付 {_money(take)}'
    else:
        take = min(amount, transit)
        transit -= take
        unpaid += take
        extra = amount - take
        if extra > 0:
            exceptions.append(_exception(record, target['证券'], 'EVENT_EXCESS', '止付确认金额超过在途金额', _money(extra)))
        change = f'在途转未付 {_money(take)}'
    target.update({'已付_CNY': _money(paid), '在途_CNY': _money(transit), '未付_CNY': _money(unpaid),
                   '超额_CNY': _money(excess), '转应收_CNY': _money(paid), '终止支付_CNY': _money(unpaid)})
    _describe(target)
    target['说明'] += '；晚到事件仅更新派生视图，冲减金额与已确认快照不变'
    return {**event, '应用结果': change}


def normalize_event(body):
    if not isinstance(body, dict):
        raise DomainError('EVENT', '事件必须为JSON对象', 400)
    record = str(body.get('record') or body.get('持仓记录') or '').strip()
    security = str(body.get('security') or body.get('证券') or '').strip()
    status = str(body.get('status') or body.get('状态') or '').strip()
    if not record:
        raise DomainError('EVENT', '事件缺少持仓编号', 422)
    if status not in (PAID, UNPAID):
        raise DomainError('EVENT', '事件状态必须为已付或未付', 422)
    raw = body.get('amount') if body.get('amount') is not None else body.get('金额')
    try:
        amount = number(raw, nullable=True)
    except DomainError:
        raise DomainError('EVENT', '事件金额无效', 422)
    if amount is None or amount <= 0:
        raise DomainError('EVENT', '事件金额必须为正', 422)
    return {
        '类型': '晚到付款' if status == PAID else '止付确认',
        '持仓记录': record, '证券': security, '状态': status,
        '金额_CNY': _money(amount.quantize(CENT)),
        '付款日期': str(body.get('paid_at') or body.get('付款日期') or ''),
        '凭证': str(body.get('voucher') or body.get('凭证') or ''),
        '说明': str(body.get('note') or body.get('说明') or ''),
    }
