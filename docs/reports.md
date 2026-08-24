# Финансовые отчёты

Production Reports используют только подтверждённые и сверенные операции текущего workspace. Draft, cancelled и deleted исключаются. Период задаётся календарными датами workspace; `date_to` включителен и преобразуется backend в начало следующего локального дня.

## API audit matrix

| Report | Existing backend calculation | Existing public API before this stage | Result |
| --- | --- | --- | --- |
| Income vs Expense | `calculations.calculate_summary` | `GET /financial-summary` | Frontend-ready; также включён в financial report |
| Cash Flow | `calculations.calculate_summary`; adjustments учитываются отдельно | `GET /financial-summary` | Frontend-ready по каждой валюте |
| Spending by Category | Частично в Telegram weekly report | Нет production API и полного refund/split учёта | Добавлен в financial report |
| Monthly Comparison | Только сравнение недель в automation report | Нет | Добавлен календарный ряд с предыдущим месяцем |
| Savings Rate | Нет согласованной доменной формулы для adjustments и отрицательного income | Нет | Requires API/domain support; не показывается |
| Net Worth / balances | `calculations.calculate_balances` | `GET /accounts/balances` | Готов только текущий snapshot; исторический net worth не заявляется |
| Largest Expenses | Можно получить из transactions, но ранжирование отсутствовало | Нет отдельного API | Добавлен backend ranking исходных expense transactions |

## Production endpoint

`GET /api/v1/reports/financial?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD&currency=RUB`

- `currency` опционален;
- без фильтра возвращаются отдельные группы RUB/EUR/USD — валюты никогда не суммируются;
- Income и Expense учитывают связанные refunds;
- Cash Flow равен `income - expense + adjustment`;
- transfer не входит в Income, Expense или Cash Flow и возвращается отдельно как `transfer_volume`;
- Spending by Category учитывает splits и пропорционально относит partial refund к категориям исходного split expense;
- Largest Expenses показывает gross amount исходной расходной операции; refunds уже учтены в KPI и category totals;
- Monthly Comparison использует локальные календарные месяцы и включает предыдущий месяц как baseline.

Frontend получает готовые денежные строки с четырьмя знаками и только форматирует их. Расчёты и объединение валют в браузере не выполняются.
