from dataclasses import dataclass
from typing import Any, Protocol

from app.integrations.google_client import GoogleClientProtocol

SHEET_NAMES = [
    "Сегодня",
    "Операции",
    "Счета",
    "Категории",
    "Бюджет",
    "Цели",
    "Долги",
    "Импорт",
    "Ошибки",
    "Конфликты",
    "Инструкция",
    "_sync_meta",
    "_lists",
]

HEADERS: dict[str, list[str]] = {
    "Операции": [
        "Дата",
        "Время",
        "Тип",
        "Сумма",
        "Валюта",
        "Счёт",
        "Счёт назначения",
        "Категория",
        "Подкатегория",
        "Контрагент",
        "Описание",
        "Комментарий",
        "Статус",
        "Источник",
        "Владелец",
        "Последнее изменение",
        "Синхронизация",
        "Ошибка",
        "_id",
        "_workspace_id",
        "_account_id",
        "_target_account_id",
        "_category_id",
        "_version",
        "_row_hash",
        "_updated_at",
        "_deleted_at",
    ],
    "Счета": [
        "Название",
        "Тип",
        "Валюта",
        "Учреждение",
        "Начальный остаток",
        "Дата начального остатка",
        "Кредитный лимит",
        "Архив",
        "Рассчитанный остаток",
        "Последнее изменение",
        "Синхронизация",
        "Ошибка",
        "_id",
        "_version",
        "_row_hash",
        "_updated_at",
        "_deleted_at",
    ],
    "Категории": [
        "Название",
        "Тип",
        "Родитель",
        "Иконка",
        "Цвет",
        "Порядок",
        "Архив",
        "Последнее изменение",
        "Синхронизация",
        "Ошибка",
        "_id",
        "_parent_id",
        "_version",
        "_row_hash",
        "_updated_at",
        "_deleted_at",
    ],
    "Ошибки": [
        "Дата",
        "Лист",
        "Строка",
        "Тип объекта",
        "Ошибка",
        "Код",
        "Рекомендуемое действие",
        "Повторить",
        "_event_id",
    ],
    "Конфликты": [
        "Тип",
        "Объект",
        "Дата конфликта",
        "Поля",
        "Значение PostgreSQL",
        "Значение Google Sheets",
        "Версия PostgreSQL",
        "Версия Google Sheets",
        "Статус",
        "Решение",
        "Ссылка в приложение",
        "_conflict_id",
    ],
}


class GoogleSheetTemplateMigrator(Protocol):
    version: int

    async def initialize(
        self,
        client: GoogleClientProtocol,
        access_token: str,
        spreadsheet_id: str,
        sheet_ids: dict[str, int],
        meta: dict[str, str],
    ) -> None: ...


@dataclass(slots=True)
class GoogleSheetTemplateV1:
    version: int = 1

    async def initialize(
        self,
        client: GoogleClientProtocol,
        access_token: str,
        spreadsheet_id: str,
        sheet_ids: dict[str, int],
        meta: dict[str, str],
    ) -> None:
        await client.values_batch_update(
            access_token,
            spreadsheet_id,
            self._initial_values(meta),
        )
        await client.batch_update(
            access_token,
            spreadsheet_id,
            self._format_requests(sheet_ids),
        )

    @staticmethod
    def _initial_values(meta: dict[str, str]) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = [
            {"range": f"'{name}'!A1", "values": [headers]} for name, headers in HEADERS.items()
        ]
        values.extend(
            [
                {
                    "range": "'Сегодня'!A1:B6",
                    "values": [
                        ["Финпространство", "Сводка только для чтения"],
                        ["Источник истины", "PostgreSQL"],
                        ["Операции", "См. лист Операции"],
                        ["Счета", "См. лист Счета"],
                        ["Категории", "См. лист Категории"],
                        ["Синхронизация", "Управляется приложением"],
                    ],
                },
                {
                    "range": "'Бюджет'!A1",
                    "values": [["Модуль бюджета будет подключён на следующем этапе."]],
                },
                {"range": "'Цели'!A1", "values": [["Модуль целей будет подключён позже."]]},
                {"range": "'Долги'!A1", "values": [["Модуль долгов будет подключён позже."]]},
                {
                    "range": "'Импорт'!A1",
                    "values": [
                        ["Импорт выполняется через проверяемый staging-процесс приложения."]
                    ],
                },
                {
                    "range": "'Инструкция'!A1:A8",
                    "values": [
                        ["Финпространство — Google Sheets"],
                        ["PostgreSQL является источником финансовой истины."],
                        ["Не редактируйте скрытые технические столбцы."],  # noqa: RUF001
                        ["Новые операции можно добавлять после установки Apps Script."],
                        ["Физическое удаление строки не удаляет операцию в приложении."],
                        ["Конфликты разрешаются в приложении."],
                        ["По умолчанию включён безопасный режим push-only."],
                        ["Секреты и OAuth-токены в этой книге не хранятся."],
                    ],
                },
                {
                    "range": "'_sync_meta'!A1:B10",
                    "values": [[key, value] for key, value in meta.items()],
                },
                {
                    "range": "'_lists'!A1:J6",
                    "values": [
                        [
                            "account_names",
                            "account_ids",
                            "category_names",
                            "category_ids",
                            "transaction_types",
                            "transaction_statuses",
                            "currency_codes",
                            "account_types",
                            "template_versions",
                            "workspace_settings",
                        ],
                        ["", "", "", "", "Доход", "Черновик", "RUB", "cash", "1", ""],
                        ["", "", "", "", "Расход", "Подтверждена", "USD", "debit_card", "", ""],
                        ["", "", "", "", "Перевод", "Сверена", "EUR", "credit_card", "", ""],
                        ["", "", "", "", "Возврат", "Отменена", "", "savings", "", ""],
                        ["", "", "", "", "Корректировка", "", "", "other", "", ""],
                    ],
                },
            ]
        )
        return values

    @staticmethod
    def _grid(
        sheet_id: int, start_column: int = 0, end_column: int | None = None
    ) -> dict[str, int]:
        grid = {"sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": start_column}
        if end_column is not None:
            grid["endColumnIndex"] = end_column
        return grid

    def _format_requests(self, sheet_ids: dict[str, int]) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        for name, headers in HEADERS.items():
            sheet_id = sheet_ids[name]
            requests.extend(
                [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheet_id,
                                "gridProperties": {"frozenRowCount": 1},
                            },
                            "fields": "gridProperties.frozenRowCount",
                        }
                    },
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": len(headers),
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": {"red": 0.12, "green": 0.23, "blue": 0.36},
                                    "textFormat": {
                                        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                        "bold": True,
                                    },
                                    "wrapStrategy": "WRAP",
                                }
                            },
                            "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy)",
                        }
                    },
                    {
                        "setBasicFilter": {
                            "filter": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": 0,
                                    "endColumnIndex": len(headers),
                                }
                            }
                        }
                    },
                    {
                        "autoResizeDimensions": {
                            "dimensions": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": 0,
                                "endIndex": len(headers),
                            }
                        }
                    },
                ]
            )
        technical = {"Операции": (18, 27), "Счета": (12, 17), "Категории": (10, 16)}
        for name, (start, end) in technical.items():
            sheet_id = sheet_ids[name]
            requests.extend(
                [
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": start,
                                "endIndex": end,
                            },
                            "properties": {"hiddenByUser": True},
                            "fields": "hiddenByUser",
                        }
                    },
                    {
                        "addProtectedRange": {
                            "protectedRange": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startColumnIndex": start,
                                    "endColumnIndex": end,
                                },
                                "description": "Технические поля Финпространства",
                                "warningOnly": False,
                            }
                        }
                    },
                ]
            )
        for name, start, end, description in (
            ("Операции", 13, 18, "Системные поля операции"),
            ("Счета", 2, 3, "Валюта существующего счёта"),
            ("Счета", 4, 7, "Начальный остаток и кредитный лимит"),
            ("Счета", 8, 12, "Рассчитанные и системные поля счёта"),
            ("Категории", 7, 10, "Системные поля категории"),
        ):
            requests.append(
                {
                    "addProtectedRange": {
                        "protectedRange": {
                            "range": {
                                "sheetId": sheet_ids[name],
                                "startColumnIndex": start,
                                "endColumnIndex": end,
                            },
                            "description": description,
                            "warningOnly": False,
                        }
                    }
                }
            )
        for name in ("Сегодня", "Ошибки", "Конфликты", "_sync_meta"):
            requests.append(
                {
                    "addProtectedRange": {
                        "protectedRange": {
                            "range": {"sheetId": sheet_ids[name]},
                            "description": "Диапазон управляется Финпространством",
                            "warningOnly": False,
                        }
                    }
                }
            )
        for hidden in ("_sync_meta", "_lists"):
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet_ids[hidden], "hidden": True},
                        "fields": "hidden",
                    }
                }
            )
        requests.extend(self._number_format_requests(sheet_ids))
        requests.extend(self._conditional_format_requests(sheet_ids))
        list_id = sheet_ids["_lists"]
        for name, column in {
            "account_names": 0,
            "category_names": 2,
            "transaction_types": 4,
            "transaction_statuses": 5,
            "currency_codes": 6,
        }.items():
            requests.append(
                {
                    "addNamedRange": {
                        "namedRange": {
                            "name": name,
                            "range": {
                                "sheetId": list_id,
                                "startRowIndex": 1,
                                "startColumnIndex": column,
                                "endColumnIndex": column + 1,
                            },
                        }
                    }
                }
            )
        requests.extend(self._validation_requests(sheet_ids))
        return requests

    @staticmethod
    def _number_format_requests(sheet_ids: dict[str, int]) -> list[dict[str, Any]]:
        formats = [
            ("Операции", 0, "DATE", "dd.MM.yyyy"),
            ("Операции", 1, "TIME", "HH:mm:ss"),
            ("Операции", 3, "NUMBER", "#,##0.00"),
            ("Операции", 15, "DATE_TIME", "dd.MM.yyyy HH:mm:ss"),
            ("Счета", 4, "NUMBER", "#,##0.00"),
            ("Счета", 5, "DATE_TIME", "dd.MM.yyyy HH:mm:ss"),
            ("Счета", 6, "NUMBER", "#,##0.00"),
            ("Счета", 8, "NUMBER", "#,##0.00"),
            ("Счета", 9, "DATE_TIME", "dd.MM.yyyy HH:mm:ss"),
            ("Категории", 7, "DATE_TIME", "dd.MM.yyyy HH:mm:ss"),
        ]
        requests = [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_ids[sheet],
                        "startRowIndex": 1,
                        "startColumnIndex": column,
                        "endColumnIndex": column + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {"type": value_type, "pattern": pattern}
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
            for sheet, column, value_type, pattern in formats
        ]
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_ids["Операции"],
                        "startRowIndex": 1,
                        "startColumnIndex": 9,
                        "endColumnIndex": 12,
                    },
                    "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                    "fields": "userEnteredFormat.wrapStrategy",
                }
            }
        )
        return requests

    @staticmethod
    def _conditional_format_requests(sheet_ids: dict[str, int]) -> list[dict[str, Any]]:
        green = {"red": 0.78, "green": 0.91, "blue": 0.80}
        amber = {"red": 1.0, "green": 0.90, "blue": 0.63}
        red = {"red": 0.96, "green": 0.75, "blue": 0.75}
        gray = {"red": 0.90, "green": 0.90, "blue": 0.90}

        def rule(
            sheet: str,
            column: int,
            condition_type: str,
            value: str,
            color: dict[str, float],
        ) -> dict[str, Any]:
            return {
                "addConditionalFormatRule": {
                    "index": 0,
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": sheet_ids[sheet],
                                "startRowIndex": 1,
                                "startColumnIndex": column,
                                "endColumnIndex": column + 1,
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": condition_type,
                                "values": [{"userEnteredValue": value}],
                            },
                            "format": {"backgroundColor": color},
                        },
                    },
                }
            }

        return [
            rule("Операции", 16, "TEXT_EQ", "SYNCED", green),
            rule("Операции", 16, "TEXT_EQ", "DIRTY", amber),
            rule("Операции", 16, "TEXT_EQ", "PENDING", amber),
            rule("Операции", 16, "TEXT_EQ", "CONFLICT", amber),
            rule("Операции", 16, "TEXT_EQ", "ERROR", red),
            rule("Операции", 16, "TEXT_EQ", "DELETED", gray),
            rule("Операции", 17, "CUSTOM_FORMULA", "=LEN($R2)>0", red),
            rule("Операции", 12, "TEXT_EQ", "Черновик", gray),
            rule("Счета", 7, "TEXT_EQ", "Да", gray),
            rule("Счета", 10, "TEXT_EQ", "SYNCED", green),
            rule("Счета", 10, "TEXT_EQ", "DIRTY", amber),
            rule("Счета", 10, "TEXT_EQ", "CONFLICT", amber),
            rule("Счета", 10, "TEXT_EQ", "ERROR", red),
            rule("Счета", 11, "CUSTOM_FORMULA", "=LEN($L2)>0", red),
            rule("Категории", 6, "TEXT_EQ", "Да", gray),
            rule("Категории", 8, "TEXT_EQ", "SYNCED", green),
            rule("Категории", 8, "TEXT_EQ", "DIRTY", amber),
            rule("Категории", 8, "TEXT_EQ", "CONFLICT", amber),
            rule("Категории", 8, "TEXT_EQ", "ERROR", red),
            rule("Категории", 9, "CUSTOM_FORMULA", "=LEN($J2)>0", red),
        ]

    @staticmethod
    def _validation_requests(sheet_ids: dict[str, int]) -> list[dict[str, Any]]:
        validations = [
            ("Операции", 2, "=transaction_types"),
            ("Операции", 4, "=currency_codes"),
            ("Операции", 5, "=account_names"),
            ("Операции", 6, "=account_names"),
            ("Операции", 7, "=category_names"),
            ("Операции", 12, "=transaction_statuses"),
        ]
        return [
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": sheet_ids[sheet],
                        "startRowIndex": 1,
                        "startColumnIndex": column,
                        "endColumnIndex": column + 1,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_RANGE",
                            "values": [{"userEnteredValue": formula}],
                        },
                        "strict": True,
                        "showCustomUi": True,
                    },
                }
            }
            for sheet, column, formula in validations
        ]
