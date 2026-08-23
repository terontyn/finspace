# Test infrastructure debt

Дата аудита: 2026-08-23.

Этот документ фиксирует предупреждения тестового стека, которые не следует устранять
случайным обновлением зависимостей внутри feature-коммита.

## Frontend: `react-test-renderer`

React 19 помечает `react-test-renderer` как deprecated и рекомендует тестировать
поведение компонентов через пользовательский DOM-интерфейс. Текущий frontend использует
React 19.2.8 и `react-test-renderer` 19.2.8. Предупреждение воспроизводится при `npm test`,
но 72 теста проходят.

Затронуты 10 component-test файлов:

- `src/components/auth-provider.test.tsx`;
- `src/components/finance-app.test.tsx`;
- `src/components/shell/shell.test.tsx`;
- `src/components/screens/account-details-screen.test.tsx`;
- `src/components/screens/account-reconciliation-dialog.test.tsx`;
- `src/components/screens/google-sheets-screen.test.tsx`;
- `src/components/screens/import-screen.test.tsx`;
- `src/components/screens/reports-screen.test.tsx`;
- `src/components/screens/sync-conflicts-screen.test.tsx`;
- `src/components/screens/today-screen.test.tsx`.

В проекте пока нет React Testing Library и DOM test environment. Безопасный отдельный
этап: добавить минимальный поддерживаемый DOM stack, перенести сначала новые Finspace
screens, сохранить поведенческие и async assertions, затем удалить renderer и его types.
Подавлять console warning нельзя.

Upstream:

- <https://react.dev/warnings/react-test-renderer>
- <https://react.dev/blog/2024/04/25/react-19-upgrade-guide>
- <https://github.com/testing-library/react-testing-library#readme>

## Backend: Starlette `TestClient` / `httpx`

Проверенный runtime:

- FastAPI 0.139.2;
- Starlette 1.3.1;
- httpx 0.28.1;
- pytest 9.1.1.

Backend suite проходит: 67 passed, 1 skipped. При импорте `fastapi.testclient.TestClient`
Starlette выдаёт `StarletteDeprecationWarning`: обычный `httpx` для TestClient deprecated,
рекомендуется `httpx2`. Это coordinated dependency migration: сначала проверить
совместимость FastAPI/Starlette, заменить test dependency и TestClient transport, затем
прогнать изолированный database runner. Blind major/transport upgrade в feature diff не
выполнялся.

Upstream:

- <https://www.starlette.io/testclient/>
- <https://github.com/Kludex/starlette/blob/main/docs/release-notes.md>
- <https://pypi.org/project/httpx2/>

## Backend tests: Mypy

`mypy app` проходит без ошибок (108 production source files). Отдельный `mypy tests`
на текущем worktree сообщает 97 ошибок в 6 файлах из 18 проверенных. Основной объём —
старые динамические fixture/helper payloads, а не дефекты production typing.

Рекомендуемый отдельный этап:

1. типизировать общие auth/entity helper return values через `TypedDict` или Pydantic;
2. заменить неоднозначные mutable dict fixtures на именованные builders;
3. исправлять тестовые файлы небольшими группами вместе с runtime assertions;
4. не добавлять массовые `# type: ignore` и не ослаблять production Mypy config.
