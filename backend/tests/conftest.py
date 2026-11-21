import os

os.environ.setdefault('AUTH_KEY', 'test-key-local')
os.environ.setdefault('SEED_DEMO', 'false')

import pytest
from sqlalchemy import text

from src.config.settings import engine
from src.models.tables import schema
from src.services import ledger


@pytest.fixture(autouse=True)
def clean_ledger():
    """每个用例前重建空账簿：清空登记表与待核池，摘除全部月分区。"""
    schema.create_all(engine)
    ledger.ensure_ledger()
    with engine.begin() as conn:
        conn.execute(text('TRUNCATE slip_index, correction_orders, month_close, audit_events'))
        conn.execute(text('TRUNCATE slip_ledger'))
        conn.execute(text('''
            DO $$
            DECLARE part record;
            BEGIN
                FOR part IN
                    SELECT child.relname AS name
                    FROM pg_catalog.pg_inherits AS edge
                    JOIN pg_catalog.pg_class AS child ON child.oid = edge.inhrelid
                    WHERE edge.inhparent = 'slip_ledger'::regclass
                      AND child.relname <> 'slip_ledger_default'
                LOOP
                    EXECUTE format('DROP TABLE %I', part.name);
                END LOOP;
            END
            $$
        '''))
    yield
