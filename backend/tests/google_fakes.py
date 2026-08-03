import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import settings
from app.integrations.google_client import GoogleIdentity, GoogleTokens


class FakeGoogleClient:
    def __init__(self) -> None:
        self.spreadsheet_id = f"fake-{uuid.uuid4()}"
        self.scopes = list(settings.google_scopes)
        self.exchange_calls = 0
        self.revoke_calls = 0
        self.batch_requests: list[dict[str, Any]] = []
        self.sheets: dict[str, list[list[Any]]] = {}

    async def exchange_code(self, code: str, verifier: str) -> GoogleTokens:
        assert code
        assert verifier
        self.exchange_calls += 1
        return GoogleTokens(
            access_token="fake-access-token",
            refresh_token="fake-refresh-token",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=self.scopes,
        )

    async def refresh_access_token(self, refresh_token: str) -> GoogleTokens:
        assert refresh_token == "fake-refresh-token"
        return GoogleTokens(
            access_token="refreshed-access-token",
            refresh_token=refresh_token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=self.scopes,
        )

    async def identity(self, access_token: str) -> GoogleIdentity:
        assert access_token
        return GoogleIdentity(subject="google-subject", email="google@example.com")

    async def revoke(self, token: str) -> None:
        assert token
        self.revoke_calls += 1

    async def create_spreadsheet(
        self,
        access_token: str,
        title: str,
        sheets: list[str],
        timezone: str,
    ) -> dict[str, Any]:
        assert access_token
        assert title.startswith("Финпространство")
        assert timezone
        return {
            "spreadsheetId": self.spreadsheet_id,
            "spreadsheetUrl": (
                f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/edit"
            ),
            "sheets": [
                {"properties": {"title": name, "sheetId": index + 10}}
                for index, name in enumerate(sheets)
            ],
        }

    async def batch_update(
        self,
        access_token: str,
        spreadsheet_id: str,
        requests: list[dict[str, Any]],
    ) -> dict[str, Any]:
        assert access_token and spreadsheet_id
        self.batch_requests.extend(requests)
        return {"replies": [{} for _ in requests]}

    @staticmethod
    def _sheet_name(range_name: str) -> str:
        return range_name.split("!", 1)[0].strip("'")

    async def values_batch_update(
        self,
        access_token: str,
        spreadsheet_id: str,
        data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        assert access_token and spreadsheet_id
        for item in data:
            range_name = str(item["range"])
            if range_name.endswith("!A2"):
                self.sheets[self._sheet_name(range_name)] = [list(row) for row in item["values"]]
        return {"totalUpdatedRows": sum(len(item.get("values", [])) for item in data)}

    @staticmethod
    def _column_index(range_name: str) -> int | None:
        cell_range = range_name.split("!", 1)[1]
        first = cell_range.split(":", 1)[0]
        letters = "".join(character for character in first if character.isalpha())
        if not letters:
            return None
        index = 0
        for character in letters:
            index = index * 26 + (ord(character.upper()) - ord("A") + 1)
        return index - 1

    @staticmethod
    def _single_column(range_name: str) -> bool:
        cell_range = range_name.split("!", 1)[1]
        parts = cell_range.split(":", 1)
        start = parts[0]
        end = parts[1] if len(parts) == 2 else start
        start_letters = "".join(character for character in start if character.isalpha())
        end_letters = "".join(character for character in end if character.isalpha())
        return bool(start_letters) and start_letters == end_letters

    async def get_values(
        self,
        access_token: str,
        spreadsheet_id: str,
        range_name: str,
    ) -> list[list[Any]]:
        assert access_token and spreadsheet_id
        rows = self.sheets.get(self._sheet_name(range_name), [])
        column = self._column_index(range_name)
        if column is not None and self._single_column(range_name):
            return [[row[column]] if len(row) > column else [] for row in rows]
        return [list(row) for row in rows]

    async def clear_values(
        self,
        access_token: str,
        spreadsheet_id: str,
        range_name: str,
    ) -> None:
        assert access_token and spreadsheet_id
        if "!A2:" in range_name:
            self.sheets[self._sheet_name(range_name)] = []
