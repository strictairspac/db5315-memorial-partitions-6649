"""在册凭条账簿：补传去重、更正单改期、月分区挂载与月结封存。

凭条账簿是持久的 PostgreSQL 范围分区表 ``slip_ledger``（按入园日分区，
``slip_ledger_default`` 为 DEFAULT 待核池）。分区表无法对凭条编号单独建
唯一约束，因此用 ``slip_index`` 索引表保证两处闸机补传记入同一本账。

并发约定：所有跨月操作先按月份升序获取 ``pg_advisory_xact_lock``，
月结只锁本月份，因此月结与无关月份的更正可以并发进行；月结登记后，
锁内核查会拒绝该月的迁入与迁出。
"""
import re
from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from src.config.settings import engine
from src.models.tables import audit, closes, corrections, slips
from src.utils.time import utc_time

PARENT = 'slip_ledger'
DEFAULT_TABLE = 'slip_ledger_default'
DEFAULT_LABEL = 'DEFAULT 待核池'
LOCK_NAMESPACE = 5315
MAX_BATCH = 500

_PARTITION_RE = re.compile(r'^slip_ledger_m(\d{4})(\d{2})$')


def _month_start(day):
    return day.replace(day=1)


def _next_month(month):
    return date(month.year + month.month // 12, month.month % 12 + 1, 1)


def _label(month):
    return f'{month.year:04d}-{month.month:02d}'


def _partition_name(month):
    return f'slip_ledger_m{month.year:04d}{month.month:02d}'


def _check_text(value, label, limit):
    if not isinstance(value, str) or not value or value != value.strip() or '\x00' in value:
        raise HTTPException(422, f'{label}须为非空文本，且不能含空字符或首尾空白')
    if len(value) > limit:
        raise HTTPException(422, f'{label}超长（至多 {limit} 字）')
    return value


def _check_month(month, label='月份'):
    if month.day != 1 or month == date(9999, 12, 1):
        raise HTTPException(422, f'{label}须为月初日期，且须有可表示的次月月初')
    return month


def ensure_ledger():
    """创建账簿父表与 DEFAULT 待核池（幂等，启动时调用）。"""
    with engine.begin() as conn:
        conn.execute(text(f'''
            CREATE TABLE IF NOT EXISTS {PARENT} (
                slip_id text NOT NULL,
                entered_on date NOT NULL,
                gate text NOT NULL,
                batch_id text NOT NULL,
                recorded_by text NOT NULL,
                updated_at text NOT NULL
            ) PARTITION BY RANGE (entered_on)
        '''))
        conn.execute(text(f'CREATE TABLE IF NOT EXISTS {DEFAULT_TABLE} PARTITION OF {PARENT} DEFAULT'))


def _log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference), created_at=utc_time()))


def _lock_months(conn, months):
    """按月份升序加事务级咨询锁；月结只锁本月份，无关月份互不阻塞。"""
    for month in sorted(set(months)):
        conn.execute(text('SELECT pg_advisory_xact_lock(:ns, :key)'),
                     {'ns': LOCK_NAMESPACE, 'key': month.year * 100 + month.month})


def _closed_months(conn):
    return set(conn.execute(select(closes.c.month)).scalars().all())


def _partition_map(conn):
    """已挂载的月分区：{月初 date: 分区表名}。DEFAULT 待核池不在其中。"""
    rows = conn.execute(text('''
        SELECT child.relname
        FROM pg_catalog.pg_inherits AS edge
        JOIN pg_catalog.pg_class AS child ON child.oid = edge.inhrelid
        WHERE edge.inhparent = pg_catalog.to_regclass(:parent)
    '''), {'parent': PARENT}).scalars().all()
    mounted = {}
    for relname in rows:
        match = _PARTITION_RE.match(relname)
        if match:
            mounted[date(int(match.group(1)), int(match.group(2)), 1)] = relname
    return mounted


def _location(month, partitions):
    relname = partitions.get(month)
    return _label(month) if relname else DEFAULT_LABEL


def ingest(batch_id, gate, rows, actor):
    """闸机分批补传：同一凭条编号全账只记一笔，重复补传原样忽略。

    逐张给出去向：recorded 已入账、duplicate 重复补传、conflict 日期不一致
    需更正单、month_closed 月份已月结。整批一个事务，重投同一批不会产生
    第二张凭条。
    """
    _check_text(batch_id, '批次号', 64)
    _check_text(gate, '闸机', 40)
    if not rows:
        raise HTTPException(422, '补传清单不能为空')
    if len(rows) > MAX_BATCH:
        raise HTTPException(422, f'单批至多 {MAX_BATCH} 张')
    seen = set()
    for row in rows:
        _check_text(row.slip_id, '凭条编号', 64)
        if row.slip_id in seen:
            raise HTTPException(422, f'批内凭条编号 {row.slip_id} 重复')
        seen.add(row.slip_id)
    months = [_month_start(row.entered_on) for row in rows]
    results = []
    with engine.begin() as conn:
        _lock_months(conn, months)
        closed = _closed_months(conn)
        partitions = _partition_map(conn)
        now = utc_time()
        for row in rows:
            month = _month_start(row.entered_on)
            stored = conn.execute(select(slips.c.entered_on).where(slips.c.slip_id == row.slip_id)).scalar()
            if stored is not None:
                status = 'duplicate' if stored == row.entered_on else 'conflict'
            elif month in closed:
                status = 'month_closed'
            else:
                try:
                    with conn.begin_nested():
                        conn.execute(slips.insert().values(slip_id=row.slip_id, entered_on=row.entered_on,
                                     gate=gate, batch_id=batch_id, recorded_by=actor, updated_at=now))
                        conn.execute(text(f'''
                            INSERT INTO {PARENT} (slip_id, entered_on, gate, batch_id, recorded_by, updated_at)
                            VALUES (:slip_id, :entered_on, :gate, :batch_id, :recorded_by, :updated_at)
                        '''), {'slip_id': row.slip_id, 'entered_on': row.entered_on, 'gate': gate,
                               'batch_id': batch_id, 'recorded_by': actor, 'updated_at': now})
                    status = 'recorded'
                except IntegrityError:
                    # 并发补传同一编号：以先落库者为准。
                    stored = conn.execute(select(slips.c.entered_on).where(slips.c.slip_id == row.slip_id)).scalar()
                    status = 'duplicate' if stored == row.entered_on else 'conflict'
            results.append({'slip_id': row.slip_id, 'entered_on': row.entered_on.isoformat(),
                            'status': status, 'location': _location(month, partitions) if status == 'recorded' else None})
        _log(conn, actor, 'ingest', batch_id)
    summary = {key: sum(item['status'] == key for item in results)
               for key in ('recorded', 'duplicate', 'conflict', 'month_closed')}
    return {'batch_id': batch_id, 'gate': gate, 'summary': summary, 'results': results}


def correct(order_id, slip_id, corrected_on, actor):
    """按凭条编号和更正单改入园日，跨月迁移在同一事务内完成。

    更正单号为幂等键：落库后回执丢失时，重试同一笔直接返回当时保存的
    回执，不改动账簿，也不会覆盖后来的更正。源月或目标月已月结则拒绝。
    """
    _check_text(order_id, '更正单号', 64)
    _check_text(slip_id, '凭条编号', 64)
    now = utc_time()
    with engine.begin() as conn:
        claim = pg_insert(corrections).values(order_id=order_id, slip_id=slip_id, from_date=corrected_on,
                to_date=corrected_on, ack={}, actor=actor, created_at=now).on_conflict_do_nothing(
                index_elements=['order_id']).returning(corrections.c.order_id)
        if conn.execute(claim).first() is None:
            stored = conn.execute(select(corrections).where(corrections.c.order_id == order_id)).mappings().one()
            if stored['slip_id'] != slip_id or stored['to_date'] != corrected_on:
                raise HTTPException(409, '更正单号已用于其他更正，请核对单号')
            return {**stored['ack'], 'replayed': True}
        slip = conn.execute(select(slips).where(slips.c.slip_id == slip_id).with_for_update()).mappings().first()
        if slip is None:
            raise HTTPException(404, f'凭条 {slip_id} 不在账')
        from_date = slip['entered_on']
        if from_date == corrected_on:
            raise HTTPException(422, '更正前后入园日期相同')
        from_month, to_month = _month_start(from_date), _month_start(corrected_on)
        _lock_months(conn, [from_month, to_month])
        closed = _closed_months(conn)
        for month, name in ((from_month, '源月份'), (to_month, '目标月份')):
            if month in closed:
                raise HTTPException(409, f'{name} {_label(month)} 已月结，凭条不得迁入或迁出')
        row = conn.execute(text(f'''
            SELECT s.gate, s.batch_id, s.recorded_by, c.relname
            FROM {PARENT} AS s JOIN pg_catalog.pg_class AS c ON c.oid = s.tableoid
            WHERE s.slip_id = :slip_id AND s.entered_on = :entered_on
            FOR UPDATE OF s
        '''), {'slip_id': slip_id, 'entered_on': from_date}).mappings().first()
        if row is None:
            raise RuntimeError(f'凭条 {slip_id} 索引与账簿不一致')
        moved = conn.execute(text(f'DELETE FROM {PARENT} WHERE slip_id = :slip_id AND entered_on = :entered_on'),
                             {'slip_id': slip_id, 'entered_on': from_date}).rowcount
        if moved != 1:
            raise RuntimeError(f'凭条 {slip_id} 迁移核对失败')
        conn.execute(text(f'''
            INSERT INTO {PARENT} (slip_id, entered_on, gate, batch_id, recorded_by, updated_at)
            VALUES (:slip_id, :entered_on, :gate, :batch_id, :recorded_by, :updated_at)
        '''), {'slip_id': slip_id, 'entered_on': corrected_on, 'gate': row['gate'],
               'batch_id': row['batch_id'], 'recorded_by': row['recorded_by'], 'updated_at': now})
        conn.execute(slips.update().where(slips.c.slip_id == slip_id)
                     .values(entered_on=corrected_on, updated_at=now))
        partitions = _partition_map(conn)
        from_label = DEFAULT_LABEL if row['relname'] == DEFAULT_TABLE else _label(from_month)
        ack = {'order_id': order_id, 'slip_id': slip_id,
               'from_date': from_date.isoformat(), 'to_date': corrected_on.isoformat(),
               'from_location': from_label, 'to_location': _location(to_month, partitions),
               'actor': actor, 'applied_at': now}
        conn.execute(corrections.update().where(corrections.c.order_id == order_id)
                     .values(from_date=from_date, ack=ack))
        _log(conn, actor, 'correct', order_id)
        return {**ack, 'replayed': False}


def register_partition(month, actor):
    """挂载月分区，并把待核池中属于该月的凭条同事务迁入。"""
    _check_month(month)
    upper = _next_month(month)
    name = _partition_name(month)
    # 表名由校验过的日期生成，仅含字母数字下划线。
    with engine.begin() as conn:
        _lock_months(conn, [month])
        if month in _closed_months(conn):
            raise HTTPException(409, f'月份 {_label(month)} 已月结，不得挂载分区迁入凭条')
        if month in _partition_map(conn):
            return {'month': _label(month), 'partition': name, 'attached': False, 'moved': 0}
        conn.execute(text(f'''
            CREATE TABLE {name} (
                LIKE {PARENT},
                CHECK (entered_on >= DATE '{month.isoformat()}' AND entered_on < DATE '{upper.isoformat()}')
            )
        '''))
        moved = conn.execute(text(f'''
            WITH moved AS (
                DELETE FROM ONLY {DEFAULT_TABLE}
                WHERE entered_on >= :lower AND entered_on < :upper
                RETURNING slip_id, entered_on, gate, batch_id, recorded_by, updated_at
            )
            INSERT INTO {name} (slip_id, entered_on, gate, batch_id, recorded_by, updated_at)
            SELECT slip_id, entered_on, gate, batch_id, recorded_by, updated_at FROM moved
            RETURNING slip_id
        '''), {'lower': month, 'upper': upper}).all()
        conn.execute(text(f'''
            ALTER TABLE {PARENT} ATTACH PARTITION {name}
            FOR VALUES FROM (DATE '{month.isoformat()}') TO (DATE '{upper.isoformat()}')
        '''))
        _log(conn, actor, 'attach_partition', _label(month))
        return {'month': _label(month), 'partition': name, 'attached': True, 'moved': len(moved)}


def close_month(month, actor):
    """办理月结：只锁本月份，完成后该月凭条不得迁入或迁出。"""
    _check_month(month, '月结月份')
    with engine.begin() as conn:
        _lock_months(conn, [month])
        stored = conn.execute(select(closes).where(closes.c.month == month)).mappings().first()
        if stored:
            return {'month': _label(month), 'closed_by': stored['closed_by'],
                    'closed_at': stored['closed_at'], 'already_closed': True}
        now = utc_time()
        conn.execute(closes.insert().values(month=month, closed_by=actor, closed_at=now))
        _log(conn, actor, 'close_month', _label(month))
        return {'month': _label(month), 'closed_by': actor, 'closed_at': now, 'already_closed': False}


def handover():
    """交班核对：从凭条账簿读取每张凭条的当前入园日与物理去向。"""
    with engine.connect() as conn:
        rows = conn.execute(text(f'''
            SELECT s.slip_id, s.entered_on, s.gate, s.batch_id, s.recorded_by, s.updated_at, c.relname
            FROM {PARENT} AS s JOIN pg_catalog.pg_class AS c ON c.oid = s.tableoid
            ORDER BY s.entered_on, s.slip_id
        ''')).mappings().all()
        partitions = _partition_map(conn)
        closed = _closed_months(conn)
        indexed = conn.execute(select(func.count()).select_from(slips)).scalar_one()
        changed = conn.execute(select(func.count()).select_from(corrections)).scalar_one()
    slips_out = []
    for row in rows:
        month = _month_start(row['entered_on'])
        location = DEFAULT_LABEL if row['relname'] == DEFAULT_TABLE else _label(month)
        slips_out.append({'slip_id': row['slip_id'], 'entered_on': row['entered_on'].isoformat(),
                          'location': location, 'gate': row['gate'], 'batch_id': row['batch_id'],
                          'recorded_by': row['recorded_by'], 'updated_at': row['updated_at']})
    counts = {month: 0 for month in partitions}
    for row in rows:
        month = _month_start(row['entered_on'])
        if month in counts:
            counts[month] += 1
    months_out = [{'month': _label(month), 'partition': partitions[month], 'slips': counts[month],
                   'closed': month in closed} for month in sorted(partitions)]
    pending = [item for item in slips_out if item['location'] == DEFAULT_LABEL]
    return {
        'today': date.today().isoformat(),
        'summary': f'账簿共 {len(slips_out)} 张凭条，待核池 {len(pending)} 张，已月结 {len(closed)} 个月份。',
        'totals': {'账簿凭条': len(slips_out), '待核池': len(pending), '月分区': len(partitions),
                   '已月结月份': len(closed), '更正单': changed},
        'consistent': len(slips_out) == indexed,
        'slips': slips_out,
        'pending': pending,
        'months': months_out,
        'closed_months': sorted(_label(month) for month in closed),
    }


def list_corrections():
    with engine.connect() as conn:
        rows = conn.execute(select(corrections).order_by(corrections.c.created_at.desc(),
                            corrections.c.order_id.desc()).limit(100)).mappings().all()
    return [{'order_id': row['order_id'], 'slip_id': row['slip_id'],
             'from_date': row['from_date'].isoformat(), 'to_date': row['to_date'].isoformat(),
             'actor': row['actor'], 'created_at': row['created_at']} for row in rows]
