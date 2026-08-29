"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { ActionDialog } from "@/components/ui/action-dialog";
import {
  BudgetForecastOccurrencesDrawer,
  BudgetForecastPanel,
  type BudgetForecastDetailsState,
  type BudgetForecastViewState,
} from "@/components/screens/budget-forecast-panel";
import { EntityDrawer } from "@/components/ui/entity-drawer";
import {
  copyBudget,
  deleteBudget,
  getBudgetHistory,
  getBudgetMonth,
  putBudget,
  restoreBudget,
} from "@/lib/budget-api";
import { getBudgetForecast } from "@/lib/budget-forecast-api";
import {
  forecastErrorMessage,
  isBudgetForecastNotFound,
  isForecastAbort,
  isForecastAuthError,
  isForecastCapabilityUnavailable,
  visualForecastPercentage,
} from "@/lib/budget-forecast";
import {
  allowedBudgetCategories,
  budgetErrorMessage,
  budgetFormFromGroup,
  budgetPeriodLabel,
  budgetRequestFromForm,
  budgetRevisionLabels,
  currentBudgetPeriod,
  formatBudgetMoney,
  initialBudgetForm,
  isNegativeMoney,
  isPositiveMoney,
  MutationIdentity,
  rolloverPolicyLabels,
  shiftBudgetPeriod,
  validateBudgetForm,
  type BudgetFormErrors,
  type BudgetFormState,
} from "@/lib/budget";
import { ApiClientError, apiClient } from "@/lib/api-client";
import type {
  AuthMeResponse,
  BudgetAllocation,
  BudgetGroup,
  BudgetRevision,
  BudgetRevisionPage,
  BudgetRolloverPolicy,
} from "@/types/budget";
import type { BudgetCategoryForecast } from "@/types/budget-forecast";
import type { Category, Paged } from "@/types/finance";

interface BudgetScreenProps {
  onError: (error: unknown) => void;
  preferredCurrency: string;
  timezone: string;
}

interface Notice {
  text: string;
  tone: "error" | "success" | "warning";
}

type PendingCommand = "copy" | "delete" | "history" | "restore" | "save" | null;
type ConfirmDialog = "copy" | "delete" | null;

const HISTORY_LIMIT = 20;

const initialForecastState: BudgetForecastViewState = {
  data: null,
  error: null,
  key: null,
  loading: false,
  stale: false,
  unavailable: false,
};

const initialForecastDetailsState: BudgetForecastDetailsState = {
  data: null,
  error: null,
  key: null,
  loading: false,
};

function moneyState(value: string): "negative" | "neutral" | "positive" {
  if (isNegativeMoney(value)) return "negative";
  if (isPositiveMoney(value)) return "positive";
  return "neutral";
}

function categoryPath(category: Category, categories: Category[]): string {
  const byId = new Map(categories.map((item) => [item.id, item]));
  const names = [category.name];
  const visited = new Set([category.id]);
  let parentId = category.parent_id;
  while (parentId && !visited.has(parentId)) {
    visited.add(parentId);
    const parent = byId.get(parentId);
    if (!parent) break;
    names.unshift(parent.name);
    parentId = parent.parent_id;
  }
  return names.join(" › ");
}

function BudgetEditor({
  categories,
  editing,
  errors,
  form,
  formError,
  isSaving,
  onChange,
  onClose,
  onSave,
}: {
  categories: Category[];
  editing: BudgetGroup | null;
  errors: BudgetFormErrors;
  form: BudgetFormState;
  formError: string | null;
  isSaving: boolean;
  onChange: (next: BudgetFormState) => void;
  onClose: () => void;
  onSave: (event: React.FormEvent) => void;
}) {
  const sortedCategories = useMemo(
    () => [...categories].sort((left, right) => left.sort_order - right.sort_order || left.name.localeCompare(right.name, "ru")),
    [categories],
  );
  const existingAllocations = useMemo(
    () => new Map(editing?.allocations.map((allocation) => [allocation.category_id, allocation]) ?? []),
    [editing],
  );
  const used = new Set(form.allocations.map((allocation) => allocation.categoryId));

  function addAllocation() {
    const category = sortedCategories.find((item) => !used.has(item.id));
    if (!category) return;
    onChange({
      ...form,
      allocations: [...form.allocations, { categoryId: category.id, note: "", plannedAmount: "" }],
    });
  }

  return <EntityDrawer ariaLabel={editing ? "Редактирование бюджета" : "Создание бюджета"} eyebrow={editing ? "Агрегат бюджета" : "Новый план"} onClose={onClose} subtitle={editing ? `Версия ${editing.version} · ${editing.currency}` : "Все итоговые показатели рассчитает backend"} title={editing ? `План на ${budgetPeriodLabel(editing.period)}` : "Создать бюджет"}>
    <form className="budget-editor-form" onSubmit={onSave}>
      {formError ? <div className="notice notice--error" role="alert">{formError}</div> : null}
      <div className="budget-form-grid">
        <label><span>Валюта</span><input aria-describedby={errors.currency ? "budget-currency-error" : undefined} disabled={Boolean(editing)} maxLength={3} onChange={(event) => onChange({ ...form, currency: event.target.value.toUpperCase() })} pattern="[A-Z]{3}" required value={form.currency}/>{errors.currency ? <small className="budget-field-error" id="budget-currency-error">{errors.currency}</small> : null}</label>
        <label><span>Плановый доход</span><input aria-describedby={errors.plannedIncome ? "budget-income-error" : undefined} inputMode="decimal" onChange={(event) => onChange({ ...form, plannedIncome: event.target.value })} placeholder="0.00" required value={form.plannedIncome}/>{errors.plannedIncome ? <small className="budget-field-error" id="budget-income-error">{errors.plannedIncome}</small> : null}</label>
      </div>
      <label><span>Переносить остаток этого месяца</span><select onChange={(event) => onChange({ ...form, rolloverPolicy: event.target.value as BudgetRolloverPolicy })} value={form.rolloverPolicy}>{Object.entries(rolloverPolicyLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><small>Это исходящая политика текущего плана, а не входящий перенос из прошлого месяца.</small></label>

      <section className="budget-form-allocations" aria-labelledby="budget-form-allocations-title">
        <div><div><span className="kicker">Exact category</span><h3 id="budget-form-allocations-title">Распределения</h3></div><button className="text-button" disabled={sortedCategories.length === used.size} onClick={addAllocation} type="button">＋ Категория</button></div>
        <p>Родительская и дочерняя категории — независимые строки. Родитель не включает подкатегории автоматически.</p>
        {errors.allocations ? <div className="budget-field-error" role="alert">{errors.allocations}</div> : null}
        {form.allocations.length ? <div className="budget-form-allocation-list">{form.allocations.map((allocation, index) => {
          const missing = !sortedCategories.some((category) => category.id === allocation.categoryId);
          const fallback = existingAllocations.get(allocation.categoryId);
          return <article key={`${allocation.categoryId}-${index}`}>
            <label><span>Категория</span><select aria-label={`Категория распределения ${index + 1}`} onChange={(event) => onChange({ ...form, allocations: form.allocations.map((item, itemIndex) => itemIndex === index ? { ...item, categoryId: event.target.value } : item) })} required value={allocation.categoryId}>{missing ? <option value={allocation.categoryId}>{fallback?.category_name ?? "Недоступная категория"} · недоступна</option> : null}{sortedCategories.map((category) => <option disabled={used.has(category.id) && category.id !== allocation.categoryId} key={category.id} value={category.id}>{categoryPath(category, categories)}</option>)}</select></label>
            <label><span>План</span><input aria-label={`Плановая сумма распределения ${index + 1}`} inputMode="decimal" onChange={(event) => onChange({ ...form, allocations: form.allocations.map((item, itemIndex) => itemIndex === index ? { ...item, plannedAmount: event.target.value } : item) })} placeholder="0.00" required value={allocation.plannedAmount}/></label>
            <label className="budget-allocation-note"><span>Примечание</span><input aria-label={`Примечание распределения ${index + 1}`} maxLength={1000} onChange={(event) => onChange({ ...form, allocations: form.allocations.map((item, itemIndex) => itemIndex === index ? { ...item, note: event.target.value } : item) })} value={allocation.note}/></label>
            <button aria-label={`Удалить распределение ${index + 1}`} className="text-button text-button--danger" onClick={() => onChange({ ...form, allocations: form.allocations.filter((_, itemIndex) => itemIndex !== index) })} type="button">Удалить строку</button>
          </article>;
        })}</div> : <div className="budget-form-empty">Распределений пока нет. Нераспределённую сумму вернёт backend.</div>}
      </section>
      <footer><button className="secondary-button" disabled={isSaving} onClick={onClose} type="button">Отмена</button><button className="primary-button" disabled={isSaving} type="submit">{isSaving ? "Сохраняем…" : editing ? "Сохранить весь план" : "Создать бюджет"}</button></footer>
    </form>
  </EntityDrawer>;
}

function SummaryCard({ currency, label, value, warning }: { currency: string; label: string; value: string; warning?: string }) {
  const state = moneyState(value);
  return <article className={`budget-summary-card budget-summary-card--${state}`}><span>{label}</span><strong>{formatBudgetMoney(value, currency)}</strong>{warning ? <small>{warning}</small> : <small>{state === "negative" ? "Отрицательное значение" : "По расчёту backend"}</small>}</article>;
}

function AllocationUsage({ allocation }: { allocation: BudgetAllocation }) {
  if (allocation.usage_percent === null) return <span className="budget-usage-empty">Процент недоступен</span>;
  const numeric = Number(allocation.usage_percent);
  if (!Number.isFinite(numeric)) return <span className="budget-usage-empty">Процент недоступен</span>;
  const over = numeric > 100;
  const width = Math.max(0, Math.min(100, numeric));
  const valueText = `${allocation.usage_percent}%${over ? " · перерасход" : ""}`;
  return <div className={`budget-usage ${over ? "budget-usage--over" : ""}`}><div aria-label="Использование бюджета" aria-valuemax={100} aria-valuemin={0} aria-valuenow={width} aria-valuetext={valueText} role="progressbar"><i style={{ width: `${width}%` }}/></div><span>{valueText}</span></div>;
}

function ForecastAllocationUsage({ category }: { category: BudgetCategoryForecast }) {
  const width = visualForecastPercentage(category.projected_usage_percent);
  if (width === null) return <span className="budget-usage-empty">Процент недоступен</span>;
  const over = isNegativeMoney(category.projected_remaining);
  const valueText = `${category.projected_usage_percent}%${over ? " · ожидается перерасход" : ""}`;
  return <div className={`budget-usage ${over ? "budget-usage--over" : ""}`}><div aria-label="Ожидаемое использование бюджета" aria-valuemax={100} aria-valuemin={0} aria-valuenow={width} aria-valuetext={valueText} role="progressbar"><i style={{ width: `${width}%` }}/></div><span>{valueText}</span></div>;
}

function AllocationList({ forecast, group }: { forecast: BudgetCategoryForecast[] | null; group: BudgetGroup }) {
  const byCategoryId = new Map(forecast?.map((category) => [category.category_id, category]) ?? []);
  const withForecast = forecast !== null;
  return <section className={`panel budget-allocation-panel ${withForecast ? "budget-allocation-panel--forecast" : ""}`}><div className="panel-heading"><div><span className="kicker">Exact-category semantics</span><h2>План по категориям</h2><p>{withForecast ? "Факт и прогноз сопоставлены только по category_id из backend." : "Родители и подкатегории показаны отдельно, без скрытого объединения."}</p></div><span className="count-badge">{group.allocations.length}</span></div>{group.allocations.length ? <div className="budget-allocation-table" role="table" aria-label={`Распределения бюджета ${group.currency}`}><div className="budget-allocation-row budget-allocation-row--head" role="row"><span>Категория</span><span>План</span><span>Факт</span>{withForecast ? <><span>Прогноз</span><span>Ожидается</span><span>Ожидаемый остаток</span></> : <span>Осталось</span>}<span>Использование</span></div>{group.allocations.map((allocation) => {
    const category = byCategoryId.get(allocation.category_id);
    const remaining = category?.projected_remaining ?? allocation.remaining;
    return <div className={`budget-allocation-row ${isNegativeMoney(remaining) ? "budget-allocation-row--over" : ""}`} key={allocation.id} role="row"><div className="budget-allocation-category" data-label="Категория"><strong>{allocation.category_name}</strong><small>{allocation.parent_id ? "Точная дочерняя категория" : "Точная категория"}{allocation.note ? ` · ${allocation.note}` : ""}</small>{allocation.category_archived || allocation.category_deleted ? <em>Категория больше недоступна</em> : null}</div><span data-label="План">{formatBudgetMoney(category?.allocated_amount ?? allocation.planned, group.currency)}</span><span data-label="Факт">{formatBudgetMoney(category?.actual_expense ?? allocation.actual, group.currency)}</span>{withForecast ? <><span data-label="Прогноз">{category ? formatBudgetMoney(category.forecast_expense, group.currency) : "—"}</span><span data-label="Ожидается">{category ? formatBudgetMoney(category.projected_expense, group.currency) : "—"}</span><span className={category && isNegativeMoney(category.projected_remaining) ? "money--negative" : undefined} data-label="Ожидаемый остаток">{category ? formatBudgetMoney(category.projected_remaining, group.currency) : "—"}{category && isNegativeMoney(category.projected_remaining) ? <small>ожидается перерасход</small> : null}</span></> : <span className={`money--${moneyState(allocation.remaining)}`} data-label="Осталось">{formatBudgetMoney(allocation.remaining, group.currency)}{isNegativeMoney(allocation.remaining) ? <small>перерасход</small> : null}</span>}<div data-label="Использование">{category ? <ForecastAllocationUsage category={category}/> : <AllocationUsage allocation={allocation}/>}</div></div>;
  })}</div> : <div className="empty-state"><strong>Распределений нет</strong><span>Плановый доход пока не назначен точным категориям.</span></div>}</section>;
}

function HistoryDrawer({
  error,
  group,
  hasMore,
  items,
  loading,
  onClose,
  onLoadMore,
}: {
  error: string | null;
  group: BudgetGroup;
  hasMore: boolean;
  items: BudgetRevision[];
  loading: boolean;
  onClose: () => void;
  onLoadMore: () => void;
}) {
  return <EntityDrawer ariaLabel="История изменений бюджета" eyebrow="Immutable revisions" onClose={onClose} subtitle={`${group.currency} · ${budgetPeriodLabel(group.period)}`} title="История изменений"><div className="budget-history-list">{error ? <div className="notice notice--error" role="alert">{error}</div> : null}{items.map((revision) => <article key={revision.id}><span className="budget-history-number">#{revision.revision_number}</span><div><strong>{budgetRevisionLabels[revision.action]}</strong><span>{new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(new Date(revision.created_at))}</span><small>actor: {revision.actor_user_id}</small></div></article>)}{!items.length && !loading ? <div className="empty-state"><strong>История пуста</strong><span>Revision появится после первой команды.</span></div> : null}{loading ? <div className="budget-history-loading">Загружаем revisions…</div> : null}{hasMore ? <button className="secondary-button" disabled={loading} onClick={onLoadMore} type="button">Загрузить ещё</button> : null}</div></EntityDrawer>;
}

export function BudgetScreen({ onError, preferredCurrency, timezone }: BudgetScreenProps) {
  const todayPeriod = useMemo(() => currentBudgetPeriod(timezone), [timezone]);
  const [period, setPeriod] = useState(todayPeriod);
  const [month, setMonth] = useState<Awaited<ReturnType<typeof getBudgetMonth>> | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [role, setRole] = useState<string>("viewer");
  const [selectedCurrency, setSelectedCurrency] = useState("");
  const [draftCurrency, setDraftCurrency] = useState(preferredCurrency.toUpperCase());
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState<PendingCommand>(null);
  const [dialog, setDialog] = useState<ConfirmDialog>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<BudgetGroup | null>(null);
  const [form, setForm] = useState<BudgetFormState>(() => initialBudgetForm(preferredCurrency));
  const [formErrors, setFormErrors] = useState<BudgetFormErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<BudgetRevision[]>([]);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [forecast, setForecast] = useState<BudgetForecastViewState>(initialForecastState);
  const [forecastRefresh, setForecastRefresh] = useState(0);
  const [forecastDetailsTargetKey, setForecastDetailsTargetKey] = useState<string | null>(null);
  const [forecastDetails, setForecastDetails] = useState<BudgetForecastDetailsState>(initialForecastDetailsState);
  const [forecastDetailsRefresh, setForecastDetailsRefresh] = useState(0);
  const requestGeneration = useRef(0);
  const forecastGeneration = useRef(0);
  const forecastDetailsGeneration = useRef(0);
  const forecastDetailsReturnFocus = useRef<HTMLElement | null>(null);
  const saveIdentity = useRef(new MutationIdentity());
  const copyIdentity = useRef(new MutationIdentity());
  const deleteIdentity = useRef(new MutationIdentity());
  const restoreIdentity = useRef(new MutationIdentity());

  const canMutate = role === "editor" || role === "owner";
  const usableCategories = useMemo(() => allowedBudgetCategories(categories), [categories]);
  const selectedGroup = useMemo(
    () => month?.groups.find((group) => group.currency === selectedCurrency) ?? null,
    [month, selectedCurrency],
  );
  const forecastRequestKey = selectedGroup && !selectedGroup.deleted_at
    ? `${period}:${selectedGroup.currency}`
    : null;
  const forecastBudgetVersion = selectedGroup?.version ?? null;
  const visibleForecast: BudgetForecastViewState = forecast.key === forecastRequestKey
    ? forecast
    : { ...initialForecastState, key: forecastRequestKey, loading: forecastRequestKey !== null };
  const forecastDetailsOpen = forecastDetailsTargetKey !== null
    && forecastDetailsTargetKey === forecastRequestKey;
  const monthFrozen = month?.projection_source === "month_close_revision";
  const modalNavigationLocked = busy !== null || dialog !== null || editorOpen || historyOpen;
  const navigationLocked = modalNavigationLocked || forecastDetailsOpen;

  const closeForecastDetails = useCallback(() => {
    const returnFocus = forecastDetailsReturnFocus.current;
    forecastDetailsReturnFocus.current = null;
    setForecastDetailsTargetKey(null);
    setForecastDetails(initialForecastDetailsState);
    queueMicrotask(() => returnFocus?.focus());
  }, []);

  const load = useCallback(async () => {
    const generation = ++requestGeneration.current;
    setLoading(true);
    setMonth(null);
    setLoadError(null);
    try {
      const [monthResult, categoryResult, me] = await Promise.all([
        getBudgetMonth(period),
        apiClient.get<Paged<Category>>("/api/v1/categories?is_archived=false&limit=500"),
        apiClient.get<AuthMeResponse>("/api/v1/auth/me"),
      ]);
      if (generation !== requestGeneration.current) return;
      setMonth(monthResult);
      setCategories(categoryResult.items);
      setRole(me.role);
      const groups = monthResult.groups;
      const preferred = groups.find((group) => group.currency === preferredCurrency.toUpperCase() && !group.deleted_at)
        ?? groups.find((group) => !group.deleted_at)
        ?? groups[0];
      setSelectedCurrency((current) => groups.some((group) => group.currency === current) ? current : preferred?.currency ?? "");
      setDraftCurrency(preferredCurrency.toUpperCase());
    } catch (error) {
      if (generation !== requestGeneration.current) return;
      const message = error instanceof ApiClientError ? error.message : "Не удалось загрузить бюджет.";
      setLoadError(message);
      onError(error);
    } finally {
      if (generation === requestGeneration.current) setLoading(false);
    }
  }, [onError, period, preferredCurrency]);

  useEffect(() => { queueMicrotask(() => void load()); }, [load]);

  useEffect(() => {
    const generation = ++forecastGeneration.current;
    let active = true;
    if (!forecastRequestKey || !selectedGroup) {
      return () => { active = false; };
    }

    const controller = new AbortController();
    queueMicrotask(() => {
      if (!active || generation !== forecastGeneration.current) return;
      setForecast((current) => ({
        data: current.key === forecastRequestKey ? current.data : null,
        error: null,
        key: forecastRequestKey,
        loading: true,
        stale: false,
        unavailable: false,
      }));
    });

    void getBudgetForecast(period, selectedGroup.currency, { signal: controller.signal }).then((result) => {
      if (!active || generation !== forecastGeneration.current) return;
      setForecast({ data: result, error: null, key: forecastRequestKey, loading: false, stale: false, unavailable: false });
    }).catch((error: unknown) => {
      if (!active || generation !== forecastGeneration.current || isForecastAbort(error)) return;
      if (isBudgetForecastNotFound(error)) {
        setForecast({ data: null, error: null, key: forecastRequestKey, loading: false, stale: false, unavailable: false });
        return;
      }
      if (isForecastCapabilityUnavailable(error)) {
        setForecast({ data: null, error: null, key: forecastRequestKey, loading: false, stale: false, unavailable: true });
        return;
      }
      const message = forecastErrorMessage(error);
      setForecast((current) => current.key === forecastRequestKey && current.data
        ? { ...current, error: message, loading: false, stale: true }
        : { data: null, error: message, key: forecastRequestKey, loading: false, stale: false, unavailable: false });
      if (isForecastAuthError(error)) onError(error);
    });

    return () => {
      active = false;
      controller.abort();
    };
  }, [forecastBudgetVersion, forecastRefresh, forecastRequestKey, onError, period, selectedGroup]);

  useEffect(() => {
    const generation = ++forecastDetailsGeneration.current;
    let active = true;
    if (
      !forecastDetailsOpen
      || !forecastRequestKey
      || forecastDetailsTargetKey !== forecastRequestKey
      || !selectedGroup
    ) return () => { active = false; };
    const controller = new AbortController();
    void getBudgetForecast(period, selectedGroup.currency, { includeOccurrences: true, signal: controller.signal }).then((result) => {
      if (!active || generation !== forecastDetailsGeneration.current) return;
      setForecastDetails({ data: result, error: null, key: forecastRequestKey, loading: false });
    }).catch((error: unknown) => {
      if (!active || generation !== forecastDetailsGeneration.current || isForecastAbort(error)) return;
      const message = forecastErrorMessage(error);
      setForecastDetails({ data: null, error: message, key: forecastRequestKey, loading: false });
      if (isForecastAuthError(error)) onError(error);
    });
    return () => {
      active = false;
      controller.abort();
    };
  }, [forecastDetailsOpen, forecastDetailsRefresh, forecastDetailsTargetKey, forecastRequestKey, onError, period, selectedGroup]);

  function applyGroup(group: BudgetGroup) {
    setMonth((current) => current ? {
      ...current,
      groups: current.groups.some((item) => item.id === group.id || item.currency === group.currency)
        ? current.groups.map((item) => item.id === group.id || item.currency === group.currency ? group : item)
        : [...current.groups, group],
      projection_source: group.projection_source,
    } : { groups: [group], historical_snapshot_available: group.projection_source === "month_close_revision", period: group.period, projection_source: group.projection_source, timezone });
    setSelectedCurrency(group.currency);
    setForecastDetailsTargetKey(null);
    setForecastDetails(initialForecastDetailsState);
    setForecastRefresh((current) => current + 1);
  }

  function retryForecast() {
    setForecastRefresh((current) => current + 1);
  }

  function openForecastDetails() {
    if (!forecastRequestKey) return;
    forecastDetailsReturnFocus.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setForecastDetails({ data: null, error: null, key: forecastRequestKey, loading: true });
    setForecastDetailsTargetKey(forecastRequestKey);
  }

  function retryForecastDetails() {
    if (!forecastRequestKey) return;
    setForecastDetails({ data: null, error: null, key: forecastRequestKey, loading: true });
    setForecastDetailsRefresh((current) => current + 1);
  }

  function changeForm(next: BudgetFormState) {
    saveIdentity.current.reset();
    setForm(next);
    setFormErrors({});
    setFormError(null);
  }

  function openCreate() {
    setEditing(null);
    setForm(initialBudgetForm(draftCurrency));
    setFormErrors({});
    setFormError(null);
    saveIdentity.current.reset();
    setEditorOpen(true);
  }

  function openEdit(group: BudgetGroup) {
    setEditing(group);
    setForm(budgetFormFromGroup(group));
    setFormErrors({});
    setFormError(null);
    saveIdentity.current.reset();
    setEditorOpen(true);
  }

  function closeEditor() {
    if (busy === "save") return;
    setEditorOpen(false);
    setEditing(null);
    setFormError(null);
    saveIdentity.current.reset();
  }

  async function reloadAfterConflict() {
    setEditorOpen(false);
    setEditing(null);
    setDialog(null);
    setDialogError(null);
    setNotice({ text: "Бюджет изменился в другой сессии. Загружена актуальная версия; старая команда не повторялась.", tone: "warning" });
    await load();
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    const errors = validateBudgetForm(form);
    setFormErrors(errors);
    if (Object.keys(errors).length) return;
    setBusy("save");
    setFormError(null);
    try {
      const result = await putBudget(period, form.currency.toUpperCase(), budgetRequestFromForm(form, editing?.version ?? null), saveIdentity.current.current());
      applyGroup(result);
      setEditorOpen(false);
      setEditing(null);
      saveIdentity.current.reset();
      setNotice({ text: editing ? "Бюджет обновлён по ответу backend." : "Бюджет создан.", tone: "success" });
    } catch (error) {
      if (error instanceof ApiClientError && error.code === "BUDGET_VERSION_CONFLICT") {
        saveIdentity.current.reset();
        await reloadAfterConflict();
      } else {
        setFormError(budgetErrorMessage(error));
      }
    } finally {
      setBusy(null);
    }
  }

  async function performCopy(overwrite: boolean) {
    const currency = selectedGroup?.currency ?? draftCurrency.toUpperCase();
    if (!/^[A-Z]{3}$/.test(currency)) {
      setNotice({ text: "Укажите трёхбуквенный код валюты перед копированием.", tone: "error" });
      return;
    }
    setBusy("copy");
    setDialogError(null);
    try {
      const result = await copyBudget(period, currency, {
        overwrite,
        ...(overwrite && selectedGroup ? { version: selectedGroup.version } : {}),
      }, copyIdentity.current.current());
      applyGroup(result);
      copyIdentity.current.reset();
      setDialog(null);
      setNotice({ text: `План ${currency} скопирован из прошлого месяца.`, tone: "success" });
    } catch (error) {
      if (error instanceof ApiClientError && error.code === "BUDGET_VERSION_CONFLICT") {
        copyIdentity.current.reset();
        await reloadAfterConflict();
      } else if (overwrite && error instanceof ApiClientError && error.status === 0) {
        setDialogError(budgetErrorMessage(error));
      } else {
        setDialog(null);
        setNotice({ text: budgetErrorMessage(error), tone: "error" });
      }
    } finally {
      setBusy(null);
    }
  }

  async function removePlan() {
    if (!selectedGroup) return;
    setBusy("delete");
    setDialogError(null);
    try {
      const result = await deleteBudget(period, selectedGroup.currency, selectedGroup.version, deleteIdentity.current.current());
      applyGroup(result);
      deleteIdentity.current.reset();
      setDialog(null);
      setNotice({ text: "План бюджета удалён мягко. Операции и фактические расходы не изменены.", tone: "success" });
    } catch (error) {
      if (error instanceof ApiClientError && error.code === "BUDGET_VERSION_CONFLICT") {
        deleteIdentity.current.reset();
        await reloadAfterConflict();
      } else if (error instanceof ApiClientError && error.status === 0) {
        setDialogError(budgetErrorMessage(error));
      } else {
        setDialog(null);
        setNotice({ text: budgetErrorMessage(error), tone: "error" });
      }
    } finally {
      setBusy(null);
    }
  }

  async function restorePlan() {
    if (!selectedGroup) return;
    setBusy("restore");
    try {
      const result = await restoreBudget(period, selectedGroup.currency, selectedGroup.version, restoreIdentity.current.current());
      applyGroup(result);
      restoreIdentity.current.reset();
      setNotice({ text: "План бюджета восстановлен.", tone: "success" });
    } catch (error) {
      if (error instanceof ApiClientError && error.code === "BUDGET_VERSION_CONFLICT") {
        restoreIdentity.current.reset();
        await reloadAfterConflict();
      } else {
        setNotice({ text: budgetErrorMessage(error), tone: "error" });
      }
    } finally {
      setBusy(null);
    }
  }

  async function loadHistory(offset: number) {
    if (!selectedGroup) return;
    setBusy("history");
    setHistoryError(null);
    try {
      const result: BudgetRevisionPage = await getBudgetHistory(period, selectedGroup.currency, offset, HISTORY_LIMIT);
      setHistory((current) => {
        const combined = offset ? [...current, ...result.items] : result.items;
        const seen = new Set<string>();
        return combined.filter((revision) => {
          if (seen.has(revision.id)) return false;
          seen.add(revision.id);
          return true;
        });
      });
      setHistoryOffset(offset + result.items.length);
      setHistoryTotal(result.page.total);
    } catch (error) {
      setHistoryError(budgetErrorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  function openHistory() {
    setHistory([]);
    setHistoryOffset(0);
    setHistoryTotal(0);
    setHistoryError(null);
    setHistoryOpen(true);
    void loadHistory(0);
  }

  function selectPeriod(next: string) {
    if (modalNavigationLocked) return;
    if (!/^\d{4}-\d{2}$/.test(next)) return;
    requestGeneration.current += 1;
    setMonth(null);
    setSelectedCurrency("");
    setNotice(null);
    saveIdentity.current.reset();
    copyIdentity.current.reset();
    deleteIdentity.current.reset();
    restoreIdentity.current.reset();
    setHistoryOpen(false);
    setHistory([]);
    setHistoryOffset(0);
    setHistoryTotal(0);
    setHistoryError(null);
    setForecastDetailsTargetKey(null);
    setForecastDetails(initialForecastDetailsState);
    setPeriod(next);
  }

  function selectCurrency(currency: string) {
    if (modalNavigationLocked) return;
    saveIdentity.current.reset();
    copyIdentity.current.reset();
    deleteIdentity.current.reset();
    restoreIdentity.current.reset();
    setHistoryOpen(false);
    setHistory([]);
    setHistoryOffset(0);
    setHistoryTotal(0);
    setHistoryError(null);
    setForecastDetailsTargetKey(null);
    setForecastDetails(initialForecastDetailsState);
    setSelectedCurrency(currency);
  }

  const forecastDetailsDrawer = forecastDetailsOpen
    ? <BudgetForecastOccurrencesDrawer onClose={closeForecastDetails} onRetry={retryForecastDetails} state={forecastDetails} timezone={forecastDetails.data?.timezone ?? timezone}/>
    : null;

  return <section className="budget-screen">
    <header className="screen-header budget-screen-header"><div><span className="kicker">Планирование без FX</span><h1>Бюджет</h1><p>Каждая валюта и точная категория планируются отдельно. Итоги и фактические значения рассчитывает backend.</p></div><div className="screen-header-actions"><button className="secondary-button" disabled={loading || navigationLocked} onClick={() => void load()} type="button">Обновить</button>{selectedGroup ? <button className="secondary-button" disabled={busy !== null} onClick={openHistory} type="button">История изменений</button> : null}{canMutate && selectedGroup && !selectedGroup.deleted_at && !selectedGroup.frozen ? <button className="primary-button" disabled={busy !== null} onClick={() => openEdit(selectedGroup)} type="button">Изменить план</button> : null}</div></header>

    <section aria-label="Выбор периода бюджета" className="panel budget-period-toolbar"><button aria-label="Предыдущий месяц" disabled={loading || navigationLocked} onClick={() => selectPeriod(shiftBudgetPeriod(period, -1))} type="button">←</button><label><span>Период бюджета</span><input aria-label="Период бюджета" disabled={loading || navigationLocked} onChange={(event) => selectPeriod(event.target.value)} type="month" value={period}/><strong>{budgetPeriodLabel(period)}</strong></label><button aria-label="Следующий месяц" disabled={loading || navigationLocked} onClick={() => selectPeriod(shiftBudgetPeriod(period, 1))} type="button">→</button><button className="text-button" disabled={period === todayPeriod || loading || navigationLocked} onClick={() => selectPeriod(todayPeriod)} type="button">Текущий месяц</button></section>

    {notice ? <div className={`notice notice--${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}><span>{notice.text}</span><button aria-label="Закрыть уведомление" className="text-button" onClick={() => setNotice(null)} type="button">×</button></div> : null}

    {loading ? <div className="budget-loading" aria-label="Загружаем бюджет"><i/><i/><i/><i/></div> : null}
    {!loading && loadError ? <section className="panel budget-load-error" role="alert"><strong>Бюджет не загрузился</strong><span>{loadError}</span><button className="secondary-button" onClick={() => void load()} type="button">Повторить</button></section> : null}

    {!loading && month && month.groups.length === 0 ? <section className="panel budget-empty-state"><div className="budget-empty-symbol">◎</div><span className="kicker">{monthFrozen ? "Закрытый месяц" : "План ещё не задан"}</span><h2>{monthFrozen ? `Бюджет на ${budgetPeriodLabel(period)} отсутствует в закрытом снимке` : `Бюджет на ${budgetPeriodLabel(period)} ещё не создан`}</h2><p>{monthFrozen ? "Месяц зафиксирован закрытием. Создание и копирование плана недоступны." : "Фактические данные не достраиваются на frontend. Создайте план или скопируйте предыдущий месяц."}</p>{!monthFrozen ? <label><span>Валюта плана</span><input aria-label="Валюта нового бюджета" maxLength={3} onChange={(event) => { copyIdentity.current.reset(); setDraftCurrency(event.target.value.toUpperCase()); }} pattern="[A-Z]{3}" value={draftCurrency}/></label> : null}{canMutate && !monthFrozen ? <div><button className="primary-button" disabled={busy !== null} onClick={openCreate} type="button">Создать бюджет</button><button className="secondary-button" disabled={busy !== null} onClick={() => void performCopy(false)} type="button">{busy === "copy" ? "Копируем…" : "Скопировать прошлый месяц"}</button></div> : <div className="budget-readonly-note">{monthFrozen ? "План зафиксирован закрытием месяца · только чтение." : "Роль viewer: создание и изменение плана недоступны."}</div>}</section> : null}

    {!loading && month && month.groups.length > 0 ? <>
      <div className="budget-currency-bar" role="tablist" aria-label="Валюты бюджета">{month.groups.map((group) => <button aria-selected={group.currency === selectedCurrency} className={group.currency === selectedCurrency ? "is-active" : ""} disabled={navigationLocked} key={group.id} onClick={() => selectCurrency(group.currency)} role="tab" type="button"><strong>{group.currency}</strong>{group.deleted_at ? <small>удалён</small> : group.frozen ? <small>закрыт</small> : <small>v{group.version}</small>}</button>)}</div>

      {selectedGroup?.deleted_at ? <section className="panel budget-deleted-state"><span className="budget-status-badge budget-status-badge--warning">Удалённый план</span><h2>План {selectedGroup.currency} удалён</h2><p>Это soft delete. Операции и фактические расходы не удалялись.</p>{selectedGroup.frozen ? <div className="budget-readonly-note">План зафиксирован закрытием месяца. Восстановление недоступно.</div> : canMutate ? <button className="primary-button" disabled={busy !== null} onClick={() => void restorePlan()} type="button">{busy === "restore" ? "Восстанавливаем…" : "Восстановить бюджет"}</button> : <div className="budget-readonly-note">Только editor или owner может восстановить план.</div>}</section> : selectedGroup ? <>
        <section className="budget-status-strip"><div><span className={`budget-status-badge ${selectedGroup.frozen ? "budget-status-badge--success" : ""}`}>{selectedGroup.frozen ? "Закрытый месяц" : "Живой план"}</span><strong>{selectedGroup.frozen ? "План зафиксирован закрытием месяца" : `Версия ${selectedGroup.version}`}</strong><small>{selectedGroup.projection_source === "month_close_revision" ? "Исторические значения из immutable month-close revision" : "Текущая backend-проекция"}</small></div>{!canMutate ? <div className="budget-readonly-note">Viewer · только чтение</div> : null}</section>

        <section aria-label={`Сводка бюджета ${selectedGroup.currency}`} className="budget-summary-grid">
          <SummaryCard currency={selectedGroup.currency} label="Плановый доход" value={selectedGroup.planned_income}/>
          <SummaryCard currency={selectedGroup.currency} label="Доступно для планирования" value={selectedGroup.planning_capacity}/>
          <SummaryCard currency={selectedGroup.currency} label="Распределено" value={selectedGroup.allocated}/>
          <SummaryCard currency={selectedGroup.currency} label="Фактический расход" value={selectedGroup.actual_expense}/>
          <SummaryCard currency={selectedGroup.currency} label="Осталось" value={selectedGroup.remaining} warning={isNegativeMoney(selectedGroup.remaining) ? "Перерасход по backend-проекции" : undefined}/>
          <SummaryCard currency={selectedGroup.currency} label="Не распределено" value={selectedGroup.unallocated} warning={isNegativeMoney(selectedGroup.unallocated) ? "План перераспределён сверх доступного" : undefined}/>
        </section>

        <section className="budget-detail-grid">
          <article className="panel budget-rollover-card"><span className="kicker">Входящий перенос</span><h2>Перенос лимита из прошлого месяца</h2><strong>{formatBudgetMoney(selectedGroup.rollover.amount, selectedGroup.currency)}</strong><p>Политика предыдущего плана: {rolloverPolicyLabels[selectedGroup.rollover.source_policy]}.</p>{selectedGroup.rollover.provisional ? <div className="budget-provisional" title="Может измениться, пока предыдущий месяц не зафиксирован."><span>◷</span><div><strong>Предварительный перенос</strong><small>Может измениться, пока предыдущий месяц не зафиксирован.</small></div></div> : <div className="budget-final-rollover">Зафиксированный входящий перенос</div>}</article>
          <article className="panel budget-rollover-card"><span className="kicker">Исходящая политика</span><h2>Переносить остаток этого месяца</h2><strong className="budget-policy-value">{rolloverPolicyLabels[selectedGroup.rollover_policy]}</strong><p>Определяет, как лимит текущего плана повлияет на следующий месяц. Это не банковский остаток.</p></article>
          <article className="panel budget-secondary-facts"><span className="kicker">Backend projection</span><dl><div><dt>Фактический доход</dt><dd>{formatBudgetMoney(selectedGroup.actual_income, selectedGroup.currency)}</dd></div><div><dt>Корректировки</dt><dd>{formatBudgetMoney(selectedGroup.adjustment, selectedGroup.currency)}</dd></div><div><dt>Net cash flow</dt><dd>{formatBudgetMoney(selectedGroup.actual_net_cashflow, selectedGroup.currency)}</dd></div><div><dt>Учтённый расход</dt><dd>{formatBudgetMoney(selectedGroup.budgeted_actual_expense, selectedGroup.currency)}</dd></div></dl></article>
        </section>

        <BudgetForecastPanel currency={selectedGroup.currency} onOpenOccurrences={openForecastDetails} onRetry={retryForecast} state={visibleForecast}/>

        {isPositiveMoney(selectedGroup.unbudgeted_actual_expense) ? <section className="budget-unbudgeted" role="status"><span>!</span><div><strong>Расходы вне распределений: {formatBudgetMoney(selectedGroup.unbudgeted_actual_expense, selectedGroup.currency)}</strong><p>Backend обнаружил расходы без Budget allocation. Frontend не распределяет их автоматически.</p></div></section> : null}

        <AllocationList forecast={visibleForecast.data?.category_forecast ?? null} group={selectedGroup}/>

        {canMutate && !selectedGroup.frozen ? <section className="panel budget-management"><div><span className="kicker">Управление планом</span><h2>Команды aggregate</h2><p>Каждая команда защищена optimistic version и отдельным idempotency key.</p></div><div><button className="secondary-button" disabled={busy !== null} onClick={() => { copyIdentity.current.reset(); setDialogError(null); setDialog("copy"); }} type="button">Скопировать прошлый месяц с заменой</button><button className="danger-button" disabled={busy !== null} onClick={() => { deleteIdentity.current.reset(); setDialogError(null); setDialog("delete"); }} type="button">Удалить план бюджета</button></div></section> : null}
      </> : null}
    </> : null}

    {editorOpen ? <BudgetEditor categories={usableCategories} editing={editing} errors={formErrors} form={form} formError={formError} isSaving={busy === "save"} onChange={changeForm} onClose={closeEditor} onSave={(event) => void save(event)}/> : null}

    {dialog === "copy" && selectedGroup ? <ActionDialog description="Backend заменит весь aggregate текущего месяца данными прошлого плана. Фактические операции не изменятся." eyebrow="Overwrite target" onClose={() => { if (!busy) { setDialog(null); setDialogError(null); copyIdentity.current.reset(); } }} title={`Заменить план ${selectedGroup.currency}?`}>{dialogError ? <div className="notice notice--error" role="alert">{dialogError}</div> : null}<div className="budget-dialog-warning"><strong>Будет отправлена версия {selectedGroup.version}</strong><span>При параллельном изменении backend вернёт version conflict; автоматического overwrite не будет.</span></div><footer><button className="secondary-button" disabled={busy !== null} onClick={() => { setDialog(null); setDialogError(null); copyIdentity.current.reset(); }} type="button">Отмена</button><button className="primary-button" disabled={busy !== null} onClick={() => void performCopy(true)} type="button">{busy === "copy" ? "Копируем…" : dialogError ? "Повторить ту же команду" : "Скопировать и заменить"}</button></footer></ActionDialog> : null}

    {dialog === "delete" && selectedGroup ? <ActionDialog description="План будет помечен удалённым и останется доступен для восстановления. Операции, категории и фактические расходы не удаляются." eyebrow="Soft delete" onClose={() => { if (!busy) { setDialog(null); setDialogError(null); deleteIdentity.current.reset(); } }} title="Удалить план бюджета?">{dialogError ? <div className="notice notice--error" role="alert">{dialogError}</div> : null}<div className="budget-dialog-warning budget-dialog-warning--danger"><strong>Удаляется только план {selectedGroup.currency}</strong><span>{budgetPeriodLabel(selectedGroup.period)} · версия {selectedGroup.version}</span></div><footer><button className="secondary-button" disabled={busy !== null} onClick={() => { setDialog(null); setDialogError(null); deleteIdentity.current.reset(); }} type="button">Отмена</button><button className="danger-button" disabled={busy !== null} onClick={() => void removePlan()} type="button">{busy === "delete" ? "Удаляем…" : dialogError ? "Повторить ту же команду" : "Удалить план бюджета"}</button></footer></ActionDialog> : null}

    {historyOpen && selectedGroup ? <HistoryDrawer error={historyError} group={selectedGroup} hasMore={historyOffset < historyTotal} items={history} loading={busy === "history"} onClose={() => setHistoryOpen(false)} onLoadMore={() => void loadHistory(historyOffset)}/> : null}
    {forecastDetailsDrawer && typeof document !== "undefined" && document.body
      ? createPortal(forecastDetailsDrawer, document.body)
      : forecastDetailsDrawer}
  </section>;
}
