from fastapi import APIRouter, Depends, Response

from src.schemas.workflow import CorrectionForm, IngestForm, MonthForm
from src.services import ledger
from src.services.security import current_user, operator

router = APIRouter(prefix='/ledger')


@router.get('/handover')
def handover_view(_=Depends(current_user)):
    return ledger.handover()


@router.get('/corrections')
def correction_list(_=Depends(current_user)):
    return ledger.list_corrections()


@router.post('/ingest', status_code=201)
def ingest_batch(form: IngestForm, actor=Depends(operator)):
    return ledger.ingest(form.batch_id, form.gate, form.rows, actor)


@router.post('/corrections', status_code=201)
def correct_slip(form: CorrectionForm, response: Response, actor=Depends(operator)):
    result = ledger.correct(form.order_id, form.slip_id, form.corrected_on, actor)
    if result['replayed']:
        response.status_code = 200
    return result


@router.post('/partitions', status_code=201)
def attach_partition(form: MonthForm, actor=Depends(operator)):
    return ledger.register_partition(form.month, actor)


@router.post('/close')
def close(form: MonthForm, actor=Depends(operator)):
    return ledger.close_month(form.month, actor)
