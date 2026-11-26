"""HTTP API used by processing workers."""

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..common import Conflict
from .rag import CONTRACT_VERSION


async def json_object(request):
    media_type = request.headers.get('content-type', '').partition(';')[0].strip().lower()
    if not (media_type == 'application/json' or (media_type.startswith('application/') and media_type.endswith('+json'))):
        raise HTTPException(400)
    try:
        value = await request.json()
    except (ValueError, RecursionError):
        raise HTTPException(400) from None
    if not isinstance(value, dict):
        raise HTTPException(400)
    return value


def routes(queue):
    def timed_response(data):
        return JSONResponse(data, headers={'X-CallOps-Server-Time': queue.service.clock().isoformat()})

    def readiness(request):
        real = request.state.worker == queue.real_worker
        data = {'real_results': real}
        if real and queue.rag is not None:
            data['rag_contract_version'] = CONTRACT_VERSION
        return timed_response(data)

    async def claim(request):
        body = await json_object(request) if await request.body() else {}
        if 'rag_contract_version' in body and type(body['rag_contract_version']) is not int:
            raise HTTPException(400)
        capable = body.get('rag_contract_version') == CONTRACT_VERSION
        result = await run_in_threadpool(queue.claim, request.state.worker, rag_capable=capable)
        return timed_response(result)

    def worker_input(request):
        data, source, version, snapshot = queue.input(request.state.worker, request.path_params['job_id'],
                                                       request.headers.get('X-Lease-Token'))
        kind = 'Snapshot' if snapshot else 'Artifact'
        return Response(data, media_type='application/json' if snapshot else 'application/octet-stream',
                        headers={f'X-Input-{kind}-ID': str(source) if source else '',
                                 f'X-Input-{kind}-Version': str(version)})

    async def report(request):
        worker, job_id = request.state.worker, request.path_params['job_id']
        action, token = request.path_params['action'], request.headers.get('X-Lease-Token')
        if action == 'result':
            data = await request.body()
            result = await run_in_threadpool(queue.upload, worker, job_id, token,
                                             request.headers.get('X-Result-Request-ID'), data)
            if 'error' in result:
                raise Conflict(result['error'])
        else:
            result = await run_in_threadpool(queue.report, worker, job_id, token, action, await json_object(request))
        return timed_response(result)

    async def cleanup(request):
        data = await json_object(request)
        return timed_response(await run_in_threadpool(queue.cleanup, request.state.worker, data or None))

    return [
        Route('/worker/readiness', readiness),
        Route('/worker/claim', claim, methods=['POST']),
        Route('/worker/jobs/{job_id:uuid}/input', worker_input),
        Route('/worker/jobs/{job_id:uuid}/{action}', report, methods=['POST']),
        Route('/worker/cleanup', cleanup, methods=['POST']),
    ]
