"""撤销决定的持久化：决定快照一次写入，后续只允许追加事件。

- 幂等键 = 基准批次 + 撤销决定编号；同键同内容重跑返回原快照（reused=True），不新增冲减。
- 同键不同内容返回 409，避免一个决定被两套口径覆盖。
- 晚到付款/止付确认写入事件表，派生视图重算，已确认快照与冲减金额不变。
"""
import hashlib, json, os, sqlite3, time
from pathlib import Path
from app.errors import DomainError
from app.modules.m8_withdrawal import rules

SCHEMA = [
    'CREATE TABLE IF NOT EXISTS withdrawals(key TEXT PRIMARY KEY, base_run TEXT NOT NULL, '
    'withdrawal_id TEXT NOT NULL, content_sha TEXT NOT NULL, snapshot TEXT NOT NULL, '
    'state TEXT NOT NULL DEFAULT "已登记", revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, '
    'closed_view TEXT)',
    'CREATE TABLE IF NOT EXISTS withdrawal_events(key TEXT NOT NULL, event_sha TEXT NOT NULL, '
    'seq INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(key,event_sha))',
]


def connection():
    path = Path(os.environ.get('APP_DB', 'data/runs.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    for statement in SCHEMA:
        db.execute(statement)
    columns = {row['name'] for row in db.execute('PRAGMA table_info(withdrawals)')}
    if 'closed_view' not in columns:
        db.execute('ALTER TABLE withdrawals ADD COLUMN closed_view TEXT')
    return db


def key_for(base_run, withdrawal_id):
    return hashlib.sha256((str(base_run) + '\n' + str(withdrawal_id).strip()).encode('utf-8')).hexdigest()[:32]


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _events(db, key):
    return [json.loads(row['payload']) for row in db.execute('SELECT payload FROM withdrawal_events WHERE key=? ORDER BY seq', (key,))]


def _row(db, key):
    return db.execute('SELECT * FROM withdrawals WHERE key=?', (key,)).fetchone()


def _load(db, key):
    row = _row(db, key)
    if row is None:
        raise DomainError('NOT_FOUND', '撤销决定不存在', 404)
    closed = json.loads(row['closed_view']) if row['closed_view'] else None
    return rules.view(json.loads(row['snapshot']), _events(db, key), row['state'], row['revision'], key, closed=closed)


def _content(base_run, withdrawal_id, reason, payments):
    entries = sorted(rules._canon(item) for item in (payments or []))
    # payments=None 表示按已复核记录派生台账，与显式空台账是不同决定。
    return rules.digest({'base': base_run, 'withdrawal_id': withdrawal_id.strip(), 'reason': str(reason or ''),
                         'source': 'derived' if payments is None else 'explicit', 'payments': entries})


def submit(base, withdrawal_id, payments, reason=''):
    base_run = base['manifest']['run_id']
    key = key_for(base_run, withdrawal_id)
    content = _content(base_run, withdrawal_id, reason, payments)
    db = connection()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = _row(db, key)
        if row:
            if row['content_sha'] != content:
                raise DomainError('WITHDRAWAL_CONFLICT', '同一撤销决定已存在且内容不同，请使用新决定编号', 409)
            closed = json.loads(row['closed_view']) if row['closed_view'] else None
            result = rules.view(json.loads(row['snapshot']), _events(db, key), row['state'], row['revision'], key,
                                reused=True, closed=closed)
            db.execute('COMMIT')
            return result
        snapshot = rules.build(base, withdrawal_id, payments, reason)
        other = db.execute('SELECT withdrawal_id FROM withdrawals WHERE base_run=? LIMIT 1', (base_run,)).fetchone()
        if other and other['withdrawal_id'] != withdrawal_id.strip():
            raise DomainError('WITHDRAWAL_EXISTS',
                              f"该批次已有撤销决定 {other['withdrawal_id']}，同一批不得再次冲减；后续到账请追加事件", 409)
        snapshot['withdrawal_key'] = key
        snapshot['content_sha256'] = content
        db.execute('INSERT INTO withdrawals(key,base_run,withdrawal_id,content_sha,snapshot,state,revision,created_at) VALUES (?,?,?,?,?,?,?,?)',
                   (key, base_run, withdrawal_id.strip(), content, json.dumps(snapshot, ensure_ascii=False), '已登记', 1, _now()))
        db.execute('COMMIT')
    except Exception:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()
    return rules.view(snapshot, [], '已登记', 1, key, reused=False)


def fetch(base_run, withdrawal_id):
    db = connection()
    try:
        return _load(db, key_for(base_run, withdrawal_id))
    finally:
        db.close()


def latest_for_run(base_run):
    db = connection()
    try:
        row = db.execute('SELECT key FROM withdrawals WHERE base_run=? ORDER BY created_at DESC, rowid DESC LIMIT 1', (base_run,)).fetchone()
        return _load(db, row['key']) if row else None
    finally:
        db.close()


def append_event(base_run, withdrawal_id, body):
    event = rules.normalize_event(body)
    key = key_for(base_run, withdrawal_id)
    event_sha = rules.digest(event)
    db = connection()
    try:
        db.execute('BEGIN IMMEDIATE')
        if _row(db, key) is None:
            raise DomainError('NOT_FOUND', '撤销决定不存在', 404)
        exists = db.execute('SELECT 1 FROM withdrawal_events WHERE key=? AND event_sha=?', (key, event_sha)).fetchone()
        if not exists:
            seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 AS n FROM withdrawal_events WHERE key=?', (key,)).fetchone()['n']
            db.execute('INSERT INTO withdrawal_events(key,event_sha,seq,payload,created_at) VALUES (?,?,?,?,?)',
                       (key, event_sha, seq, json.dumps(event, ensure_ascii=False), _now()))
        result = _load(db, key)
        db.execute('COMMIT')
    except Exception:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()
    result['event_reused'] = bool(exists)
    return result


def close(base_run, withdrawal_id, revision):
    key = key_for(base_run, withdrawal_id)
    db = connection()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = _row(db, key)
        if row is None:
            raise DomainError('NOT_FOUND', '撤销决定不存在', 404)
        current = _load(db, key)
        if row['state'] == '已结批':
            current['close_reused'] = True
            db.execute('COMMIT')
            return current
        if not current['can_close']:
            raise DomainError('NOT_CLOSABLE', '本批尚有未决事项：' + '；'.join(current['blockers']), 409)
        events = _events(db, key)
        frozen = rules.freeze(current, events)
        changed = db.execute('UPDATE withdrawals SET state=?,revision=revision+1,closed_view=? WHERE key=? AND revision=?',
                             ('已结批', json.dumps(frozen, ensure_ascii=False), key, revision)).rowcount
        if not changed:
            raise DomainError('VERSION', '版本已变化，请重新载入', 409)
        result = _load(db, key)
        db.execute('COMMIT')
    except Exception:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()
    result['close_reused'] = False
    return result


def history(base_run=None):
    db = connection()
    try:
        if base_run:
            rows = db.execute('SELECT key,base_run,withdrawal_id,state,revision,created_at FROM withdrawals WHERE base_run=? ORDER BY rowid DESC', (base_run,))
        else:
            rows = db.execute('SELECT key,base_run,withdrawal_id,state,revision,created_at FROM withdrawals ORDER BY rowid DESC')
        return [dict(row) for row in rows]
    finally:
        db.close()
