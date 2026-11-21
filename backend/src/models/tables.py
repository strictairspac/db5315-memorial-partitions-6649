from sqlalchemy import (JSON, Column, Date, ForeignKey, Integer, MetaData,
                        String, Table, Text)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))
# 在册凭条索引：与分区账簿 slip_ledger 同事务写入，凭条编号全账唯一。
slips = Table('slip_index', schema,
    Column('slip_id', String(64), primary_key=True),
    Column('entered_on', Date, nullable=False),
    Column('gate', String(40), nullable=False),
    Column('batch_id', String(64), nullable=False),
    Column('recorded_by', String(40), nullable=False),
    Column('updated_at', String(32), nullable=False))
# 更正单：order_id 为幂等键，ack 保存落库时的回执，重试原样返回。
corrections = Table('correction_orders', schema,
    Column('order_id', String(64), primary_key=True),
    Column('slip_id', String(64), nullable=False, index=True),
    Column('from_date', Date, nullable=False),
    Column('to_date', Date, nullable=False),
    Column('ack', JSON, nullable=False),
    Column('actor', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
# 月结登记：已月结月份的凭条不得迁入或迁出。
closes = Table('month_close', schema,
    Column('month', Date, primary_key=True),
    Column('closed_by', String(40), nullable=False),
    Column('closed_at', String(32), nullable=False))
