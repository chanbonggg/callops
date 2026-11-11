"""Browser entry point for transcript search."""

from pathlib import Path

from starlette.routing import Route
from starlette.templating import Jinja2Templates

from ..calls.service import CALL_TIMEZONE
from ..common import Conflict
from .service import MAX_QUERY_LENGTH, matching_segments


TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name('templates'))
FORM_ERRORS = {
    'INVALID_SEARCH_QUERY': '검색어를 1~200자로 입력하세요.',
    'INVALID_COUNTERPARTY': '거래처 이름을 100자 이내로 입력하세요.',
    'INVALID_CALL_FILTER': '통화 시작일과 종료일을 확인하세요.',
}


def routes(service):
    def search(request):
        fields = {'q': '', 'counterparty': '', 'from_date': '', 'to_date': ''}
        context = {
            'fields': fields, 'csrf': request.state.login['csrf'], 'query_limit': MAX_QUERY_LENGTH,
            'call_timezone': CALL_TIMEZONE,
        }
        status_code = 200
        if request.method == 'POST':
            fields.update({name: request.state.form.get(name, '') for name in fields})
            try:
                matches, limited, unavailable = matching_segments(service, **fields)
                context.update(matches=matches, limited=limited, unavailable=unavailable)
            except Conflict as error:
                if str(error) not in FORM_ERRORS:
                    raise
                context['error'] = FORM_ERRORS[str(error)]
                status_code = 400
        return TEMPLATES.TemplateResponse(request, 'search.html', context, status_code=status_code)

    return [Route('/search', search, methods=['GET', 'POST'])]
