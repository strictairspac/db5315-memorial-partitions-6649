from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI
import uvicorn

from scripts.seed_demo import seed, seed_ledger
from src.config.settings import IS_MYSQL, engine
from src.middlewares.request import limit_body, request_headers
from src.models.tables import schema
from src.routes.ledger import router as ledger_router
from src.routes.workflow import router
from src.services import ledger


@asynccontextmanager
async def lifespan(app):
    schema.create_all(engine)
    if not IS_MYSQL:  # 在册账簿依赖 PostgreSQL 范围分区
        ledger.ensure_ledger()
    if os.getenv('SEED_DEMO', 'true').lower() == 'true':
        try:
            seed()
            if not IS_MYSQL:
                seed_ledger()
        except Exception:
            logging.exception('Demo seeding failed')
    yield


app = FastAPI(title='Database business workflow', lifespan=lifespan)
app.middleware('http')(limit_body)
app.middleware('http')(request_headers)
app.include_router(router)
app.include_router(ledger_router)

if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8000)
