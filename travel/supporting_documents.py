"""Private application-level documents, kept separate from traveller ticket emails."""

import logging
import uuid
from contextlib import contextmanager
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import boto3
from botocore.config import Config
from django.conf import settings

from .models import SupportingDocument

logger = logging.getLogger(__name__)


class DocumentStorageError(Exception):
    pass


def client():
    if not settings.TRAVEL_S3_BUCKET or not settings.TRAVEL_S3_REGION:
        raise DocumentStorageError("Supporting document storage is not configured.")
    return boto3.client(
        "s3",
        region_name=settings.TRAVEL_S3_REGION,
        config=Config(connect_timeout=3, read_timeout=15, retries={"total_max_attempts": 2}),
    )


def validate_uploads(request):
    files = request.FILES.getlist("supporting_documents")
    if len(files) > 10 or sum(f.size for f in files) > 25 * 1024 * 1024:
        raise ValueError("Attach at most 10 supporting documents, up to 25 MB in total.")
    prepared = []
    for f in files:
        if not 0 < f.size <= 10 * 1024 * 1024 or len(f.name) > 200:
            raise ValueError(
                "Each supporting document must be nonempty, up to 10 MB, with a filename of at most 200 characters."
            )
        content = f.read()
        ext = Path(f.name).suffix.lower()
        mime = None
        if ext == ".pdf" and content.startswith(b"%PDF-"):
            mime = "application/pdf"
        elif ext == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif ext in [".jpg", ".jpeg"] and content.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif ext == ".msg" and content.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
            mime = "application/vnd.ms-outlook"
        elif ext == ".docx":
            try:
                with ZipFile(BytesIO(content)) as z:
                    if (
                        "[Content_Types].xml" in z.namelist()
                        and "word/document.xml" in z.namelist()
                    ):
                        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            except BadZipFile:
                pass
        elif ext in [".txt", ".eml"]:
            try:
                content.decode("utf-8")
                if ext == ".txt":
                    mime = "text/plain"
                elif BytesParser().parsebytes(content, headersonly=True).get("From"):
                    mime = "message/rfc822"
            except UnicodeDecodeError:
                pass
        if not mime:
            raise ValueError(
                "Supporting files must be PDF, PNG, JPEG, DOCX, TXT, EML or Outlook MSG documents matching their file type."
            )
        prepared.append((f.name, mime, content))
    return prepared


@contextmanager
def document_batch():
    uploaded = []
    try:
        yield uploaded
    except Exception:
        for key in uploaded:
            try:
                client().delete_object(Bucket=settings.TRAVEL_S3_BUCKET, Key=key)
            except Exception:
                logger.error("Supporting document cleanup failed; review S3 orphan objects.")
        raise


def save_documents(trip, files, uploaded):
    for filename, mime, content in files:
        key = f"{settings.TRAVEL_S3_PREFIX}/{trip.pk}/{uuid.uuid4().hex}"
        uploaded.append(key)
        encryption = (
            {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": settings.TRAVEL_S3_KMS_KEY_ID}
            if settings.TRAVEL_S3_KMS_KEY_ID
            else {"ServerSideEncryption": "AES256"}
        )
        try:
            client().put_object(
                Bucket=settings.TRAVEL_S3_BUCKET,
                Key=key,
                Body=content,
                ContentType=mime,
                **encryption,
            )
        except Exception as exc:
            logger.warning("Supporting document upload failed (%s).", type(exc).__name__)
            raise DocumentStorageError(
                "Could not store supporting documents. Your request was not saved. Please try again."
            ) from exc
        SupportingDocument.objects.create(
            request=trip, filename=filename, content_type=mime, size=len(content), object_key=key
        )


def download_url(document):
    from django.utils.http import content_disposition_header

    return client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.TRAVEL_S3_BUCKET,
            "Key": document.object_key,
            "ResponseContentDisposition": content_disposition_header(True, document.filename),
            "ResponseContentType": "application/octet-stream",
        },
        ExpiresIn=settings.TRAVEL_S3_DOWNLOAD_TTL,
    )
