from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from src.config.settings import IS_MYSQL, engine
from src.constants.workflow import Role
from src.models.tables import audit, cases, evidence, runs, schema, slips, users
from src.services import ledger
from src.services.domain import SPEC
from src.services.security import hash_password
from src.utils.time import utc_time


def keep_existing(conn, table, key, values):
    if IS_MYSQL:
        statement = mysql_insert(table).values(**values).on_duplicate_key_update(id=table.c.id)
    else:
        statement = postgres_insert(table).values(**values).on_conflict_do_nothing(index_elements=[key])
    conn.execute(statement)


def seed():
    with engine.begin() as conn:
        for role in Role:
            keep_existing(conn, users, 'username', {'username': role.value, 'role': role.value,
                'password_hash': hash_password('Demo-Walkthrough-2026')})
        keep_existing(conn, cases, 'slug', {'slug': 'demo', 'title': SPEC['name'],
            'payload': SPEC['sample'], 'author': 'demo', 'created_at': utc_time()})
        return {table.name: conn.execute(select(func.count()).select_from(table)).scalar_one()
                for table in (users, cases, runs, evidence, audit)}


if __name__ == '__main__':
    schema.create_all(engine)
    print(seed())


def seed_ledger():
    """首次启动时在册账簿演示：两处闸机补传，一张漏建月份进待核池。"""
    if IS_MYSQL:
        return
    from src.schemas.workflow import IngestRow

    ledger.ensure_ledger()
    with engine.connect() as conn:
        if conn.execute(select(func.count()).select_from(slips)).scalar_one():
            return
    ledger.register_partition(date(2026, 10, 1), 'demo')
    ledger.register_partition(date(2026, 11, 1), 'demo')
    east = [IngestRow(slip_id='SM-1031-01', entered_on=date(2026, 10, 31)),
            IngestRow(slip_id='SM-1101-01', entered_on=date(2026, 11, 1))]
    west = [IngestRow(slip_id='SM-1121-01', entered_on=date(2026, 11, 21)),
            IngestRow(slip_id='SM-0915-01', entered_on=date(2026, 9, 15))]
    ledger.ingest('SEED-GATE-EAST', '东岗', east, 'demo')
    ledger.ingest('SEED-GATE-WEST', '西岗', west, 'demo')
