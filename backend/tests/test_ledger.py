"""在册凭条账簿：补传去重、更正迁移、月结并发与幂等回执的核验。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from src.config.settings import engine
from src.schemas.workflow import IngestRow
from src.services import ledger

M08, M09, M10, M11 = date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)


def rows(*pairs):
    return [IngestRow(slip_id=slip_id, entered_on=day) for slip_id, day in pairs]


def physical():
    """账簿物理归属：{凭条编号: (入园日, 分区表)}，直接读 tableoid。"""
    with engine.connect() as conn:
        found = conn.execute(text('''
            SELECT s.slip_id, s.entered_on, c.relname
            FROM slip_ledger AS s JOIN pg_catalog.pg_class AS c ON c.oid = s.tableoid
        ''')).all()
    return {row.slip_id: (row.entered_on, row.relname) for row in found}


def locations(hand):
    return {item['slip_id']: item['location'] for item in hand['slips']}


def test_two_gates_ingest_into_one_ledger_without_duplicates():
    ledger.register_partition(M10, 'op')
    ledger.register_partition(M11, 'op')
    first = ledger.ingest('B-EAST', '东岗', rows(('SM-01', date(2026, 10, 5)), ('SM-02', date(2026, 11, 2))), 'op')
    assert first['summary'] == {'recorded': 2, 'duplicate': 0, 'conflict': 0, 'month_closed': 0}
    # 另一处闸机补传同一批，其中一张入园日填错
    second = ledger.ingest('B-WEST', '西岗', rows(
        ('SM-01', date(2026, 10, 5)), ('SM-02', date(2026, 11, 2)), ('SM-03', date(2026, 11, 3))), 'op')
    assert second['summary'] == {'recorded': 1, 'duplicate': 2, 'conflict': 0, 'month_closed': 0}
    wrong = ledger.ingest('B-WEST-2', '西岗', rows(('SM-01', date(2026, 10, 6))), 'op')
    assert wrong['results'][0]['status'] == 'conflict'
    # 重投第一批仍是幂等的
    again = ledger.ingest('B-EAST', '东岗', rows(('SM-01', date(2026, 10, 5)), ('SM-02', date(2026, 11, 2))), 'op')
    assert again['summary']['duplicate'] == 2
    hand = ledger.handover()
    assert hand['totals']['账簿凭条'] == 3 and hand['consistent'] is True
    assert len(physical()) == 3
    assert locations(hand) == {'SM-01': '2026-10', 'SM-02': '2026-11', 'SM-03': '2026-11'}


def test_cross_month_correction_moves_partition():
    ledger.register_partition(M10, 'op')
    ledger.register_partition(M11, 'op')
    ledger.ingest('B-1', '东岗', rows(('SM-01', date(2026, 10, 5))), 'op')
    ack = ledger.correct('CJ-1', 'SM-01', date(2026, 11, 5), 'op')
    assert ack['replayed'] is False
    assert (ack['from_location'], ack['to_location']) == ('2026-10', '2026-11')
    assert physical()['SM-01'] == (date(2026, 11, 5), 'slip_ledger_m202611')
    assert locations(ledger.handover()) == {'SM-01': '2026-11'}


def test_migration_is_atomic_to_concurrent_readers():
    """迁移在同一事务内删除并插入：其他会话只能看到迁移前或迁移后的那一张。"""
    ledger.register_partition(M10, 'op')
    ledger.register_partition(M11, 'op')
    ledger.ingest('B-1', '东岗', rows(('SM-01', date(2026, 10, 5))), 'op')
    with engine.connect() as conn:  # 另一会话持有未提交的迁移
        tx = conn.begin()
        conn.execute(text("DELETE FROM slip_ledger WHERE slip_id = 'SM-01'"))
        conn.execute(text('''
            INSERT INTO slip_ledger (slip_id, entered_on, gate, batch_id, recorded_by, updated_at)
            VALUES ('SM-01', '2026-11-05', '东岗', 'B-1', 'op', '2026-11-21T08:55:00+00:00')
        '''))
        before = physical()  # 本会话仍看到迁移前的那一张
        assert before['SM-01'] == (date(2026, 10, 5), 'slip_ledger_m202610')
        assert len(before) == 1
        tx.commit()
    after = physical()  # 提交后只看到迁移后的那一张
    assert after['SM-01'] == (date(2026, 11, 5), 'slip_ledger_m202611')
    assert len(after) == 1


def test_correction_into_unregistered_month_lands_in_pending_pool():
    ledger.register_partition(M10, 'op')
    ledger.ingest('B-1', '东岗', rows(('SM-01', date(2026, 10, 5))), 'op')
    ack = ledger.correct('CJ-1', 'SM-01', date(2026, 9, 15), 'op')
    assert ack['to_location'] == ledger.DEFAULT_LABEL
    assert physical()['SM-01'] == (date(2026, 9, 15), 'slip_ledger_default')
    hand = ledger.handover()
    assert [item['slip_id'] for item in hand['pending']] == ['SM-01']


def test_correction_replay_returns_original_ack_without_overwriting_later_correction():
    for month in (M09, M10, M11):
        ledger.register_partition(month, 'op')
    ledger.ingest('B-1', '东岗', rows(('SM-01', date(2026, 9, 15))), 'op')
    first = ledger.correct('CJ-1', 'SM-01', date(2026, 10, 5), 'op')
    ledger.correct('CJ-2', 'SM-01', date(2026, 11, 5), 'op')
    # 回执丢失后重试同一笔：拿回当时的回执，后来的更正不被覆盖
    replay = ledger.correct('CJ-1', 'SM-01', date(2026, 10, 5), 'op')
    assert replay['replayed'] is True
    assert all(replay[key] == value for key, value in first.items() if key != 'replayed')
    assert physical()['SM-01'] == (date(2026, 11, 5), 'slip_ledger_m202611')
    # 同一单号换了参数视为另一笔，拒绝
    with pytest.raises(HTTPException) as error:
        ledger.correct('CJ-1', 'SM-01', date(2026, 10, 6), 'op')
    assert error.value.status_code == 409


def test_closed_month_blocks_migration_in_and_out():
    ledger.register_partition(M09, 'op')
    ledger.register_partition(M10, 'op')
    ledger.ingest('B-1', '东岗', rows(('SM-01', date(2026, 9, 15)), ('SM-02', date(2026, 10, 5))), 'op')
    done = ledger.close_month(M09, 'op')
    assert done['already_closed'] is False
    assert ledger.close_month(M09, 'op')['already_closed'] is True
    with pytest.raises(HTTPException) as out:  # 迁出已月结月份
        ledger.correct('CJ-1', 'SM-01', date(2026, 10, 5), 'op')
    assert out.value.status_code == 409
    with pytest.raises(HTTPException) as into:  # 迁入已月结月份
        ledger.correct('CJ-2', 'SM-02', date(2026, 9, 20), 'op')
    assert into.value.status_code == 409
    # 未月结月份的更正照常
    ack = ledger.correct('CJ-3', 'SM-02', date(2026, 10, 20), 'op')
    assert ack['to_location'] == '2026-10'
    # 补传到已月结月份逐张拒收，其余入账
    late = ledger.ingest('B-LATE', '西岗', rows(('SM-03', date(2026, 9, 10)), ('SM-04', date(2026, 10, 10))), 'op')
    assert {item['slip_id']: item['status'] for item in late['results']} == {
        'SM-03': 'month_closed', 'SM-04': 'recorded'}
    assert len(physical()) == 3


def test_month_close_and_unrelated_corrections_run_concurrently():
    ledger.register_partition(M09, 'op')
    ledger.register_partition(M10, 'op')
    ledger.register_partition(M11, 'op')
    ledger.ingest('B-1', '东岗', rows(('SM-01', date(2026, 10, 5)), ('SM-02', date(2026, 11, 2))), 'op')
    with engine.connect() as conn:  # 另一会话持有 9 月月结锁，模拟月结进行中
        tx = conn.begin()
        conn.execute(text('SELECT pg_advisory_xact_lock(:ns, :key)'),
                     {'ns': ledger.LOCK_NAMESPACE, 'key': 202609})
        with ThreadPoolExecutor(max_workers=2) as pool:  # 无关月份的更正不等月结
            one = pool.submit(ledger.correct, 'CJ-A', 'SM-01', date(2026, 10, 20), 'op')
            two = pool.submit(ledger.correct, 'CJ-B', 'SM-02', date(2026, 10, 25), 'op')
            assert one.result(timeout=10)['to_location'] == '2026-10'
            assert two.result(timeout=10)['to_location'] == '2026-10'
        tx.rollback()
    assert ledger.close_month(M09, 'op')['already_closed'] is False  # 月结照常办完
    with pytest.raises(HTTPException) as error:  # 月结后迁入被拒
        ledger.correct('CJ-C', 'SM-01', date(2026, 9, 5), 'op')
    assert error.value.status_code == 409


def test_close_and_correction_stress_stays_consistent():
    for month in (M09, M10, M11):
        ledger.register_partition(month, 'op')
    slips = [(f'SM-{index:02d}', date(2026, 10, index + 1)) for index in range(8)]
    ledger.ingest('B-1', '东岗', rows(*slips), 'op')
    ledger.ingest('B-2', '西岗', rows(*((f'SM-1{index}', date(2026, 11, index + 1)) for index in range(8))), 'op')
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(ledger.close_month, M09, 'op')]
        futures += [pool.submit(ledger.correct, f'CJ-{index}', f'SM-{index:02d}', date(2026, 11, index + 2), 'op')
                    for index in range(8)]
        futures += [pool.submit(ledger.correct, f'CJ-1{index}', f'SM-1{index}', date(2026, 10, index + 3), 'op')
                    for index in range(8)]
        results = [future.result(timeout=30) for future in futures]
    assert results[0]['month'] == '2026-09'
    assert all(not ack['replayed'] for ack in results[1:])
    hand = ledger.handover()
    assert hand['totals']['账簿凭条'] == 16 and hand['consistent'] is True
    assert len(physical()) == 16


def test_register_partition_backfills_pending_pool():
    ledger.ingest('B-1', '东岗', rows(('SM-01', date(2026, 8, 15)), ('SM-02', date(2026, 8, 20))), 'op')
    assert physical()['SM-01'][1] == 'slip_ledger_default'
    assert ledger.handover()['totals']['待核池'] == 2
    attached = ledger.register_partition(M08, 'op')
    assert attached['attached'] is True and attached['moved'] == 2
    assert physical()['SM-01'] == (date(2026, 8, 15), 'slip_ledger_m202608')
    assert physical()['SM-02'] == (date(2026, 8, 20), 'slip_ledger_m202608')
    assert ledger.handover()['totals']['待核池'] == 0
    again = ledger.register_partition(M08, 'op')  # 重复挂载幂等
    assert again['attached'] is False and again['moved'] == 0


def test_handover_reconciles_current_date_and_location():
    ledger.register_partition(M10, 'op')
    ledger.register_partition(M11, 'op')
    ledger.ingest('B-1', '东岗', rows(
        ('SM-01', date(2026, 10, 5)), ('SM-02', date(2026, 11, 2)), ('SM-03', date(2026, 9, 15))), 'op')
    ledger.correct('CJ-1', 'SM-01', date(2026, 11, 5), 'op')
    hand = ledger.handover()
    assert hand['consistent'] is True
    assert hand['totals'] == {'账簿凭条': 3, '待核池': 1, '月分区': 2, '已月结月份': 0, '更正单': 1}
    assert locations(hand) == {'SM-01': '2026-11', 'SM-02': '2026-11', 'SM-03': ledger.DEFAULT_LABEL}
    assert [item['slip_id'] for item in hand['pending']] == ['SM-03']
    assert len(physical()) == 3


def test_validation_rejects_bad_input():
    with pytest.raises(HTTPException) as not_month:
        ledger.register_partition(date(2026, 8, 2), 'op')
    assert not_month.value.status_code == 422
    with pytest.raises(HTTPException) as missing:
        ledger.correct('CJ-X', 'SM-NONE', date(2026, 10, 1), 'op')
    assert missing.value.status_code == 404
    ledger.register_partition(M10, 'op')
    ledger.ingest('B-1', '东岗', rows(('SM-01', date(2026, 10, 5))), 'op')
    with pytest.raises(HTTPException) as unchanged:
        ledger.correct('CJ-2', 'SM-01', date(2026, 10, 5), 'op')
    assert unchanged.value.status_code == 422
    with pytest.raises(HTTPException) as empty:
        ledger.ingest('B-2', '东岗', [], 'op')
    assert empty.value.status_code == 422
    with pytest.raises(HTTPException) as repeated:
        ledger.ingest('B-3', '东岗', rows(('SM-02', date(2026, 10, 6)), ('SM-02', date(2026, 10, 7))), 'op')
    assert repeated.value.status_code == 422
    # 失败的更正不占用单号，修正后可用同一单号重新提交
    with pytest.raises(HTTPException):
        ledger.correct('CJ-9', 'SM-GONE', date(2026, 10, 8), 'op')
    ack = ledger.correct('CJ-9', 'SM-01', date(2026, 10, 8), 'op')
    assert ack['replayed'] is False and ack['to_date'] == '2026-10-08'
