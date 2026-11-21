"""在册账簿 HTTP 接口：登录、权限、补传、更正回执、月结与交班核对。"""
from fastapi.testclient import TestClient

from scripts.seed_demo import seed
from src.main import app

PASSWORD = 'Demo-Walkthrough-2026'


def login(client, username):
    response = client.post('/login', json={'username': username, 'password': PASSWORD})
    assert response.status_code == 200
    return {'Authorization': f"Bearer {response.json()['token']}"}


def test_ledger_api_flow():
    seed()
    with TestClient(app) as client:
        operator = login(client, 'operator')
        viewer = login(client, 'viewer')
        # 观察员可以看账，不能补传
        assert client.get('/ledger/handover', headers=viewer).status_code == 200
        forbidden = client.post('/ledger/ingest', headers=viewer, json={
            'batch_id': 'B-V', 'gate': '东岗', 'rows': [{'slip_id': 'SM-V', 'entered_on': '2026-10-01'}]})
        assert forbidden.status_code == 403
        # 经办员登记 10 月分区，非月初日期被拒
        assert client.post('/ledger/partitions', headers=operator, json={'month': '2026-10-01'}).status_code == 201
        assert client.post('/ledger/partitions', headers=operator, json={'month': '2026-10-02'}).status_code == 422
        # 两处闸机分批补传同一本账
        east = client.post('/ledger/ingest', headers=operator, json={'batch_id': 'B-E', 'gate': '东岗', 'rows': [
            {'slip_id': 'SM-1', 'entered_on': '2026-10-05'}, {'slip_id': 'SM-2', 'entered_on': '2026-10-06'}]})
        assert east.status_code == 201 and east.json()['summary']['recorded'] == 2
        west = client.post('/ledger/ingest', headers=operator, json={'batch_id': 'B-W', 'gate': '西岗', 'rows': [
            {'slip_id': 'SM-1', 'entered_on': '2026-10-05'}, {'slip_id': 'SM-3', 'entered_on': '2026-10-07'}]})
        assert west.json()['summary'] == {'recorded': 1, 'duplicate': 1, 'conflict': 0, 'month_closed': 0}
        # 更正落库拿到回执；重试同一笔拿回同一回执（200 且 applied_at 相同）
        form = {'order_id': 'CJ-1', 'slip_id': 'SM-1', 'corrected_on': '2026-11-05'}
        first = client.post('/ledger/corrections', headers=operator, json=form)
        assert first.status_code == 201 and first.json()['replayed'] is False
        replay = client.post('/ledger/corrections', headers=operator, json=form)
        assert replay.status_code == 200 and replay.json()['replayed'] is True
        assert replay.json()['applied_at'] == first.json()['applied_at']
        # 11 月未建分区，更正后凭条在待核池；交班核对能看到当前日期与去向
        hand = client.get('/ledger/handover', headers=viewer).json()
        current = {item['slip_id']: item for item in hand['slips']}
        assert current['SM-1']['entered_on'] == '2026-11-05'
        assert current['SM-1']['location'] == 'DEFAULT 待核池'
        assert hand['consistent'] is True
        # 月结 11 月后，该月凭条不得迁出
        assert client.post('/ledger/close', headers=operator, json={'month': '2026-11-01'}).status_code == 200
        blocked = client.post('/ledger/corrections', headers=operator, json={
            'order_id': 'CJ-2', 'slip_id': 'SM-1', 'corrected_on': '2026-10-20'})
        assert blocked.status_code == 409
        # 无关月份（10 月）的更正仍能继续
        other = client.post('/ledger/corrections', headers=operator, json={
            'order_id': 'CJ-3', 'slip_id': 'SM-2', 'corrected_on': '2026-10-20'})
        assert other.status_code == 201
        # 更正单历史可查
        orders = client.get('/ledger/corrections', headers=viewer).json()
        assert [row['order_id'] for row in orders] == ['CJ-3', 'CJ-1']
