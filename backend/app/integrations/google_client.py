from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote

import httpx


@dataclass(frozen=True, slots=True)
class GoogleTokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scopes: list[str]


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    subject: str
    email: str | None


class GoogleApiError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 502, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class GoogleClientProtocol(Protocol):
    async def exchange_code(self, code: str, verifier: str) -> GoogleTokens: ...
    async def refresh_access_token(self, refresh_token: str) -> GoogleTokens: ...
    async def identity(self, access_token: str) -> GoogleIdentity: ...
    async def revoke(self, token: str) -> None: ...
    async def create_spreadsheet(
        self, access_token: str, title: str, sheets: list[str], timezone: str
    ) -> dict[str, Any]: ...
    async def batch_update(
        self, access_token: str, spreadsheet_id: str, requests: list[dict[str, Any]]
    ) -> dict[str, Any]: ...
    async def values_batch_update(
        self, access_token: str, spreadsheet_id: str, data: list[dict[str, Any]]
    ) -> dict[str, Any]: ...
    async def get_values(
        self, access_token: str, spreadsheet_id: str, range_name: str
    ) -> list[list[Any]]: ...
    async def clear_values(
        self, access_token: str, spreadsheet_id: str, range_name: str
    ) -> None: ...


class GoogleRestClient:
    def __init__(
        self, *, client_id: str, client_secret: str, redirect_uri: str, timeout: float = 30.0
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.timeout = timeout

    @staticmethod
    async def _payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.is_success:
            return payload if isinstance(payload, dict) else {}
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        reason = error.get("status") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else str(error or "")
        retryable = response.status_code == 429 or response.status_code >= 500
        if response.status_code == 401:
            code = "GOOGLE_TOKEN_REVOKED"
        elif response.status_code == 403:
            code = "GOOGLE_PERMISSION_DENIED"
        elif response.status_code == 404:
            code = "GOOGLE_SHEET_NOT_FOUND"
        elif response.status_code == 429:
            code = "GOOGLE_API_RATE_LIMITED"
        else:
            code = "GOOGLE_SYNC_EVENT_FAILED"
        raise GoogleApiError(
            code,
            str(message or reason or f"Google API returned HTTP {response.status_code}"),
            status_code=502,
            retryable=retryable,
        )

    async def exchange_code(self, code: str, verifier: str) -> GoogleTokens:
        response = await self._public_request(
            "POST",
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri,
            },
        )
        payload = await self._payload(response)
        return _tokens(payload)

    async def refresh_access_token(self, refresh_token: str) -> GoogleTokens:
        response = await self._public_request(
            "POST",
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        payload = await self._payload(response)
        tokens = _tokens(payload)
        return GoogleTokens(tokens.access_token, refresh_token, tokens.expires_at, tokens.scopes)

    async def identity(self, access_token: str) -> GoogleIdentity:
        response = await self._public_request(
            "GET",
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        payload = await self._payload(response)
        subject = str(payload.get("sub", ""))
        if not subject:
            raise GoogleApiError("GOOGLE_PERMISSION_DENIED", "Google identity is missing")
        email = str(payload["email"]) if payload.get("email") else None
        return GoogleIdentity(subject=subject, email=email)

    async def revoke(self, token: str) -> None:
        response = await self._public_request(
            "POST",
            "https://oauth2.googleapis.com/revoke",
            params={"token": token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code not in {200, 400}:
            await self._payload(response)

    async def create_spreadsheet(
        self, access_token: str, title: str, sheets: list[str], timezone: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "https://sheets.googleapis.com/v4/spreadsheets",
            access_token,
            json={
                "properties": {"title": title, "locale": "ru_RU", "timeZone": timezone},
                "sheets": [{"properties": {"title": sheet}} for sheet in sheets],
            },
        )

    async def batch_update(
        self, access_token: str, spreadsheet_id: str, requests: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet_id)}:batchUpdate",
            access_token,
            json={"requests": requests},
        )

    async def values_batch_update(
        self, access_token: str, spreadsheet_id: str, data: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet_id)}/values:batchUpdate",
            access_token,
            json={"valueInputOption": "USER_ENTERED", "data": data},
        )

    async def get_values(
        self, access_token: str, spreadsheet_id: str, range_name: str
    ) -> list[list[Any]]:
        encoded_range = quote(range_name, safe="")
        payload = await self._request(
            "GET",
            (
                "https://sheets.googleapis.com/v4/spreadsheets/"
                f"{quote(spreadsheet_id)}/values/{encoded_range}?majorDimension=ROWS"
            ),
            access_token,
        )
        values = payload.get("values", [])
        return values if isinstance(values, list) else []

    async def clear_values(self, access_token: str, spreadsheet_id: str, range_name: str) -> None:
        encoded_range = quote(range_name, safe="")
        await self._request(
            "POST",
            (
                "https://sheets.googleapis.com/v4/spreadsheets/"
                f"{quote(spreadsheet_id)}/values/{encoded_range}:clear"
            ),
            access_token,
            json={},
        )

    async def _request(
        self, method: str, url: str, access_token: str, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    method,
                    url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                    **kwargs,
                )
        except httpx.TimeoutException as exc:
            raise GoogleApiError(
                "GOOGLE_SYNC_EVENT_FAILED", "Google API timeout", retryable=True
            ) from exc
        except httpx.NetworkError as exc:
            raise GoogleApiError(
                "GOOGLE_SYNC_EVENT_FAILED", "Google API network error", retryable=True
            ) from exc
        return await self._payload(response)

    async def _public_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                return await client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise GoogleApiError(
                "GOOGLE_SYNC_EVENT_FAILED", "Google OAuth timeout", retryable=True
            ) from exc
        except httpx.NetworkError as exc:
            raise GoogleApiError(
                "GOOGLE_SYNC_EVENT_FAILED", "Google OAuth network error", retryable=True
            ) from exc


def _tokens(payload: dict[str, Any]) -> GoogleTokens:
    access_token = str(payload.get("access_token", ""))
    if not access_token:
        raise GoogleApiError("GOOGLE_TOKEN_EXPIRED", "Google token response has no access token")
    expires_in = int(payload.get("expires_in", 3600))
    raw_scopes = payload.get("scope", "")
    scopes = str(raw_scopes).split() if raw_scopes else []
    refresh_token = str(payload["refresh_token"]) if payload.get("refresh_token") else None
    return GoogleTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        scopes=scopes,
    )
