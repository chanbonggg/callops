"""HTTP API used by processing workers."""

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


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
        return timed_response({'real_results': request.state.worker == queue.real_worker})

    def claim(request):
        return timed_response(queue.claim(request.state.worker))

    def worker_input(request):
        data, artifact, version = queue.input(request.state.worker, request.path_params['job_id'],
                                              request.headers.get('X-Lease-Token'))
        return Response(data, media_type='application/octet-stream',
                        headers={'X-Input-Artifact-ID': str(artifact) if artifact else '',
                                 'X-Input-Artifact-Version': str(version)})

    async def report(request):
        worker, job_id = request.state.worker, request.path_params['job_id']
        action, token = request.path_params['action'], request.headers.get('X-Lease-Token')
        if action == 'result':
            data = await request.body()
            result = await run_in_threadpool(queue.upload, worker, job_id, token,
                                             request.headers.get('X-Result-Request-ID'), data)
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
