"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { apiClient } from "@/lib/api-client";
import type { ApiPage, RecurringExecution, RecurringRule } from "@/types/automations";
import type { Account, Category, Paged } from "@/types/finance";

type RuleType = "expense" | "income" | "transfer";

export function RecurringRulesScreen({ onError }: { onError: (error: unknown) => void }) {
  const auth = useAuth();
  const [rules, setRules] = useState<RecurringRule[]>([]);
  const [historyRuleId, setHistoryRuleId] = useState<string | null>(null);
  const [history, setHistory] = useState<RecurringExecution[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [name, setName] = useState("Ежемесячная операция");
  const [type, setType] = useState<RuleType>("expense");
  const [amount, setAmount] = useState("1000.00");
  const [accountId, setAccountId] = useState("");
  const [targetAccountId, setTargetAccountId] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const [frequency, setFrequency] = useState("MONTHLY");
  const [monthDay, setMonthDay] = useState("1");
  const [weekDay, setWeekDay] = useState("MO");
  const [hour, setHour] = useState("9");
  const [mode, setMode] = useState("draft");

  const load = useCallback(async () => {
    try {
      const [ruleResult, accountResult, categoryResult] = await Promise.all([
        apiClient.get<ApiPage<RecurringRule>>("/api/v1/recurring-rules"),
        apiClient.get<Paged<Account>>("/api/v1/accounts?is_archived=false&limit=200"),
        apiClient.get<Paged<Category>>("/api/v1/categories?is_archived=false&limit=500"),
      ]);
      setRules(ruleResult.items);
      setAccounts(accountResult.items);
      setCategories(categoryResult.items);
      setAccountId((current) => current || accountResult.items[0]?.id || "");
      setTargetAccountId((current) => current || accountResult.items[1]?.id || "");
    } catch (error) {
      onError(error);
    }
  }, [onError]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  const matchingCategories = useMemo(
    () => categories.filter((item) => item.category_type === type || item.category_type === "both"),
    [categories, type],
  );
  const effectiveCategoryId = matchingCategories.some((item) => item.id === categoryId)
    ? categoryId
    : (matchingCategories[0]?.id ?? "");

  async function createRule() {
    const account = accounts.find((item) => item.id === accountId);
    if (!account) return;
    const schedule = frequency === "MONTHLY"
      ? `FREQ=MONTHLY;BYMONTHDAY=${monthDay};BYHOUR=${hour};BYMINUTE=0`
      : frequency === "WEEKLY"
        ? `FREQ=WEEKLY;BYDAY=${weekDay};BYHOUR=${hour};BYMINUTE=0`
        : `FREQ=DAILY;BYHOUR=${hour};BYMINUTE=0`;
    try {
      await apiClient.post("/api/v1/recurring-rules", {
        name,
        rule_type: type,
        transaction_type: type,
        schedule_rrule: schedule,
        timezone: auth.session?.workspace.timezone ?? "Europe/Amsterdam",
        amount,
        currency: account.currency,
        account_id: accountId,
        target_account_id: type === "transfer" ? targetAccountId : null,
        category_id: type === "transfer" ? null : effectiveCategoryId,
        creation_mode: mode,
        days_before_reminder: 0,
      });
      await load();
    } catch (error) {
      onError(error);
    }
  }

  async function action(rule: RecurringRule, name: "pause" | "resume" | "run-now") {
    try {
      await apiClient.post(`/api/v1/recurring-rules/${rule.id}/${name}`, {});
      await load();
      if (historyRuleId === rule.id) await loadHistory(rule.id);
    } catch (error) {
      onError(error);
    }
  }

  async function loadHistory(ruleId: string) {
    try {
      const result = await apiClient.get<ApiPage<RecurringExecution>>(
        `/api/v1/recurring-rules/${ruleId}/history?limit=20`,
      );
      setHistoryRuleId(ruleId);
      setHistory(result.items);
    } catch (error) {
      onError(error);
    }
  }

  return (
    <section>
      <header className="screen-header"><div><span className="kicker">Расписание Backend</span><h1>Регулярные операции</h1><p>n8n передаёт только ID правила и время; сумма и счета берутся из сохранённого правила.</p></div></header>
      <div className="two-column">
        <article className="panel">
          <div className="panel-heading"><div><span className="kicker">Правила</span><h2>Активные расписания</h2></div><span className="count-badge">{rules.length}</span></div>
          <div className="card-list">
            {rules.map((rule) => (
              <div className="automation-row recurring-row" key={rule.id}>
                <div><strong>{rule.name}</strong><small>{rule.amount} {rule.currency} · {rule.creation_mode}<br />Следующий: {formatDate(rule.next_run_at)}</small></div>
                <div className="compact-actions">
                  <button className="text-button" type="button" onClick={() => void action(rule, rule.is_active ? "pause" : "resume")}>{rule.is_active ? "Пауза" : "Продолжить"}</button>
                  <button className="text-button" type="button" onClick={() => void action(rule, "run-now")}>Выполнить</button>
                  <button className="text-button" type="button" onClick={() => void loadHistory(rule.id)}>История</button>
                </div>
              </div>
            ))}
            {!rules.length ? <div className="empty-state">Создайте первое правило.</div> : null}
          </div>
          {historyRuleId ? (
            <div className="history-panel">
              <strong>Последние исполнения</strong>
              {history.map((item) => (
                <div className="automation-row" key={item.id}>
                  <small>{formatDate(item.scheduled_for)}</small>
                  <span className={`status-chip status-chip--${item.status}`}>{item.status}</span>
                </div>
              ))}
              {!history.length ? <div className="empty-state">Исполнений пока нет.</div> : null}
            </div>
          ) : null}
        </article>

        <form className="form-panel" onSubmit={(event) => { event.preventDefault(); void createRule(); }}>
          <div><span className="kicker">Новое правило</span><h2>Понятное расписание</h2></div>
          <label>Название<input value={name} onChange={(event) => setName(event.target.value)} /></label>
          <div className="form-grid">
            <label>Тип<select value={type} onChange={(event) => setType(event.target.value as RuleType)}><option value="expense">Расход</option><option value="income">Доход</option><option value="transfer">Перевод</option></select></label>
            <label>Сумма<input inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} /></label>
            <label>Счёт<select value={accountId} onChange={(event) => setAccountId(event.target.value)}>{accounts.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
            {type === "transfer" ? <label>Куда<select value={targetAccountId} onChange={(event) => setTargetAccountId(event.target.value)}>{accounts.filter((item) => item.id !== accountId).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label> : <label>Категория<select value={effectiveCategoryId} onChange={(event) => setCategoryId(event.target.value)}>{matchingCategories.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}
            <label>Период<select value={frequency} onChange={(event) => setFrequency(event.target.value)}><option value="DAILY">Каждый день</option><option value="WEEKLY">Каждую неделю</option><option value="MONTHLY">Каждый месяц</option></select></label>
            {frequency === "WEEKLY" ? <label>День<select value={weekDay} onChange={(event) => setWeekDay(event.target.value)}><option value="MO">Понедельник</option><option value="TU">Вторник</option><option value="WE">Среда</option><option value="TH">Четверг</option><option value="FR">Пятница</option><option value="SA">Суббота</option><option value="SU">Воскресенье</option></select></label> : null}
            {frequency === "MONTHLY" ? <label>Число<input type="number" min="1" max="31" value={monthDay} onChange={(event) => setMonthDay(event.target.value)} /></label> : null}
            <label>Час<input type="number" min="0" max="23" value={hour} onChange={(event) => setHour(event.target.value)} /></label>
            <label>Режим<select value={mode} onChange={(event) => setMode(event.target.value)}><option value="draft">Черновик</option><option value="confirmed">Сразу подтвердить</option><option value="reminder_only">Только напомнить</option></select></label>
          </div>
          <button type="submit" disabled={!accountId || (type !== "transfer" && !effectiveCategoryId)}>Создать правило</button>
        </form>
      </div>
    </section>
  );
}

function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString("ru-RU") : "не запланировано";
}
