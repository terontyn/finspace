from typing import Annotated

from fastapi import Depends

from app.core.config import settings
from app.core.errors import ApiError
from app.integrations.google_client import GoogleClientProtocol, GoogleRestClient


def get_google_client() -> GoogleClientProtocol:
    client_id = settings.google_client_id_value
    client_secret = settings.google_client_secret_value
    if not settings.google_is_configured or not client_id or not client_secret:
        raise ApiError(
            status_code=503,
            code="GOOGLE_NOT_CONFIGURED",
            message="Google OAuth is not configured",
        )
    return GoogleRestClient(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=settings.google_redirect_uri,
    )


GoogleClient = Annotated[GoogleClientProtocol, Depends(get_google_client)]
