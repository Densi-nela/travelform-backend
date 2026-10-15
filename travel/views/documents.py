"""Department lookup and authorized supporting-document downloads."""

from django.http import HttpResponseRedirect, JsonResponse
from django.views.decorators.http import require_GET

from ..models import Department, SupportingDocument, TravelRequest
from ..roles import is_travel_admin
from ..supporting_documents import download_url
from .common import error


@require_GET
def department_options(request):
    qs = Department.objects.filter(active=True, name__icontains=request.GET.get("search", "")[:150])
    return JsonResponse({"results": list(qs.values("id", "name")[:30])})


@require_GET
def supporting_document_download(request, pk, document_id, token=None):
    if token is not None:
        allowed = (
            TravelRequest.objects.filter(pk=pk, approval_token=token)
            .exclude(supervisor_status="not_required")
            .exists()
        )
    else:
        allowed = is_travel_admin(request.user)
    if not allowed:
        return error("Access denied.", 403)
    document = SupportingDocument.objects.filter(pk=document_id, request_id=pk).first()
    if document is None:
        return error("Document not found.", 404)
    try:
        response = HttpResponseRedirect(download_url(document))
        response["Cache-Control"] = "no-store"
        response["Referrer-Policy"] = "no-referrer"
        return response
    except Exception:
        return error("Document storage is unavailable. Please try again later.", 503)
