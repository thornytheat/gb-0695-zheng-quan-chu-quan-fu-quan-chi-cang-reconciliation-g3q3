from flask import Blueprint, Response, jsonify, request
import csv, io
from app.errors import DomainError
from app.store import get_run, latest
from app import withdrawal_store


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise DomainError('REQUEST', '请求内容必须为JSON对象', 400)
    return body


def _context(body):
    base_run = body.get('base_run') or body.get('run_id')
    base = get_run(base_run) if base_run else latest()
    if base is None:
        raise DomainError('NOT_FOUND', '基准批次不存在', 404)
    withdrawal_id = body.get('withdrawal_id') or body.get('id')
    if not isinstance(withdrawal_id, str) or not withdrawal_id.strip():
        raise DomainError('WITHDRAWAL', '撤销决定编号不能为空', 422)
    return base, withdrawal_id


bp = Blueprint('m8', __name__)


@bp.route('/api/withdrawal', methods=['POST'])
def create():
    body = _body()
    base, withdrawal_id = _context(body)
    return jsonify(withdrawal_store.submit(base, withdrawal_id, body.get('payments'), body.get('reason')))


@bp.route('/api/withdrawal/<ident>')
def fetch(ident):
    base_run = request.args.get('run_id')
    if not base_run:
        raise DomainError('REQUEST', '缺少run_id', 422)
    return jsonify(withdrawal_store.fetch(base_run, ident))


@bp.route('/api/withdrawals')
def listing():
    return jsonify(withdrawal_store.history(request.args.get('run_id')))


@bp.route('/api/withdrawal/events', methods=['POST'])
def event():
    body = _body()
    base, withdrawal_id = _context(body)
    return jsonify(withdrawal_store.append_event(base['manifest']['run_id'], withdrawal_id, body.get('event') or body))


@bp.route('/api/withdrawal/close', methods=['POST'])
def close():
    body = _body()
    base, withdrawal_id = _context(body)
    try:
        revision = int(body.get('revision', 0))
    except (ValueError, TypeError):
        raise DomainError('VERSION', '版本必须为整数')
    return jsonify(withdrawal_store.close(base['manifest']['run_id'], withdrawal_id, revision))


@bp.route('/api/withdrawal/evidence.csv')
def evidence():
    """撤销证据单独导出，不改写基准批次的证据CSV。"""
    base_run = request.args.get('run_id')
    withdrawal_id = request.args.get('withdrawal_id')
    if not base_run or not withdrawal_id:
        raise DomainError('REQUEST', '缺少run_id或withdrawal_id', 422)
    result = withdrawal_store.fetch(base_run, withdrawal_id)
    base = get_run(base_run)
    rule_version = base['manifest']['rule_version']
    rows = []
    for key, value in result['totals'].items():
        rows.append({'区段': '合计', '持仓记录': key, '金额_CNY': value})
    for row in result['rows']:
        rows.append({'区段': '冲减', **{k: v for k, v in row.items() if k != '凭证明细'}})
    for row in result['outside']:
        rows.append({'区段': '非本批付款', **row})
    for row in result['exceptions']:
        rows.append({'区段': '异常', **row})
    for row in result['events']:
        rows.append({'区段': '事件', **row})
    fields = list(dict.fromkeys(['区段', 'run_id', 'withdrawal_id', '规则版本', '状态'] + [k for row in rows for k in row]))
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({'run_id': base_run, 'withdrawal_id': result['withdrawal_id'],
                         '规则版本': rule_version, '状态': result['state'], **row})
    return Response('﻿' + out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=withdrawal-evidence.csv'})
