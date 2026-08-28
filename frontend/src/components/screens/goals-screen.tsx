"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ActionDialog } from "@/components/ui/action-dialog";
import { EntityDrawer } from "@/components/ui/entity-drawer";
import { ApiClientError, apiClient } from "@/lib/api-client";
import {
  addGoalContribution,
  correctGoalContribution,
  createGoal,
  deleteGoal,
  getGoal,
  listGoalContributions,
  listGoals,
  restoreGoal,
  runGoalLifecycle,
  updateGoal,
} from "@/lib/goals-api";
import {
  formatGoalMoney,
  formatGoalTargetDate,
  goalCreateRequest,
  goalDeadlineLabel,
  goalErrorMessage,
  goalFormFromRecord,
  GoalMutationIdentity,
  goalProgressPresentation,
  goalStatusLabels,
  goalUpdateRequest,
  initialGoalForm,
  isUncertainGoalError,
  normalizeGoalMoney,
  validateGoalForm,
  type GoalFormErrors,
  type GoalFormState,
} from "@/lib/goals";
import type {
  Goal,
  GoalAuthMeResponse,
  GoalContribution,
  GoalStatus,
} from "@/types/goals";

interface GoalsScreenProps {
  onError: (error: unknown) => void;
  preferredCurrency: string;
}

interface Notice {
  text: string;
  tone: "error" | "success" | "warning";
}

type LifecycleOperation = "pause" | "resume" | "complete" | "reopen" | "cancel";
type MutationScope =
  | "create"
  | "update"
  | "contribution"
  | "correction"
  | "pause"
  | "resume"
  | "complete"
  | "reopen"
  | "cancel"
  | "delete"
  | "restore";

const PAGE_LIMIT = 12;
const HISTORY_LIMIT = 20;
const CONFIRMED_REFRESH_WARNING = "Команда выполнена, но актуальные данные загрузить не удалось. Не повторяйте мутацию: используйте «Обновить» или откройте цель повторно.";
const refreshRequiredCodes = new Set([
  "GOAL_CONTRIBUTION_NOT_ALLOWED",
  "GOAL_CURRENCY_IMMUTABLE",
  "GOAL_NOT_FOUND",
  "GOAL_RESTORE_REQUIRED",
  "GOAL_STATUS_INVALID",
  "GOAL_TARGET_NOT_REACHED",
]);

const lifecycleLabels: Record<LifecycleOperation, string> = {
  cancel: "Отменить цель",
  complete: "Завершить",
  pause: "Приостановить",
  reopen: "Вернуть в активные",
  resume: "Возобновить",
};

function GoalProgress({ goal }: { goal: Goal }) {
  const progress = goalProgressPresentation(goal.progress_percent);
  return <div className="goals-progress">
    <div
      aria-label={`Прогресс цели ${goal.name}`}
      aria-valuemax={100}
      aria-valuemin={0}
      aria-valuenow={progress.visualValue}
      aria-valuetext={`${goal.progress_percent}%`}
      role="progressbar"
    ><i style={{ width: `${progress.visualValue}%` }}/></div>
    <span>{progress.exactText}</span>
  </div>;
}

function GoalStateNotes({ goal }: { goal: Goal }) {
  return <div className="goals-state-notes">
    {goal.deleted_at ? <span className="goals-state-note goals-state-note--deleted">Удалена · lifecycle сохранён: {goalStatusLabels[goal.status]}</span> : null}
    {!goal.deleted_at && goal.is_target_reached && goal.status !== "completed" ? <span className="goals-state-note goals-state-note--reached">Цель достигнута · lifecycle: {goalStatusLabels[goal.status]}</span> : null}
    {!goal.deleted_at && goal.status === "completed" && !goal.is_target_reached ? <span className="goals-state-note goals-state-note--warning">Завершена, но после исправления прогресс ниже цели</span> : null}
  </div>;
}

function GoalCard({ goal, onOpen }: { goal: Goal; onOpen: (goal: Goal) => void }) {
  return <article className={`goals-card goals-card--${goal.status} ${goal.deleted_at ? "goals-card--deleted" : ""}`} data-goal-id={goal.id}>
    <header><div><span className={`goals-status goals-status--${goal.status}`}>{goalStatusLabels[goal.status]}</span>{goal.deleted_at ? <span className="goals-deleted-badge">Удалена</span> : null}</div><span className="goals-currency">{goal.currency}</span></header>
    <div className="goals-card-copy"><h2>{goal.name}</h2>{goal.description ? <p>{goal.description}</p> : <p className="goals-muted">Без описания</p>}</div>
    <GoalStateNotes goal={goal}/>
    <GoalProgress goal={goal}/>
    <dl className="goals-money-grid">
      <div><dt>Цель</dt><dd>{formatGoalMoney(goal.target_amount, goal.currency)}</dd></div>
      <div><dt>Внесено</dt><dd>{formatGoalMoney(goal.contributed_amount, goal.currency)}</dd></div>
      <div className={goal.remaining_amount.startsWith("-") ? "is-negative" : ""}><dt>Осталось</dt><dd>{formatGoalMoney(goal.remaining_amount, goal.currency)}</dd></div>
    </dl>
    <footer><div><strong>{goalDeadlineLabel(goal)}</strong><span>{goal.target_date ? formatGoalTargetDate(goal.target_date) : "Дата не задана"}</span></div><button className="secondary-button" onClick={() => onOpen(goal)} type="button">Открыть</button></footer>
  </article>;
}

function GoalEditor({
  editing,
  errors,
  form,
  formError,
  isSaving,
  onChange,
  onClose,
  onSave,
}: {
  editing: Goal | null;
  errors: GoalFormErrors;
  form: GoalFormState;
  formError: string | null;
  isSaving: boolean;
  onChange: (form: GoalFormState) => void;
  onClose: () => void;
  onSave: (event: React.FormEvent) => void;
}) {
  const currencyLocked = Boolean(editing && editing.contribution_count > 0);
  return <EntityDrawer ariaLabel={editing ? "Редактирование цели" : "Создание цели"} eyebrow={editing ? "Optimistic aggregate" : "Новая цель"} onClose={onClose} subtitle={editing ? `Версия ${editing.version}` : "Начальный lifecycle назначит backend"} title={editing ? editing.name : "Создать цель"}>
    <form className="goals-editor-form" onSubmit={onSave}>
      {formError ? <div className="notice notice--error" role="alert">{formError}</div> : null}
      <label><span>Название</span><input aria-describedby={errors.name ? "goal-name-error" : undefined} autoComplete="off" maxLength={200} onChange={(event) => onChange({ ...form, name: event.target.value })} required value={form.name}/>{errors.name ? <small className="goals-field-error" id="goal-name-error">{errors.name}</small> : null}</label>
      <label><span>Описание</span><textarea maxLength={2000} onChange={(event) => onChange({ ...form, description: event.target.value })} rows={4} value={form.description}/></label>
      <div className="goals-form-grid">
        <label><span>Валюта</span><input aria-describedby={errors.currency ? "goal-currency-error" : currencyLocked ? "goal-currency-hint" : undefined} disabled={currencyLocked} maxLength={3} onChange={(event) => onChange({ ...form, currency: event.target.value.toUpperCase() })} pattern="[A-Z]{3}" required value={form.currency}/>{errors.currency ? <small className="goals-field-error" id="goal-currency-error">{errors.currency}</small> : null}{currencyLocked ? <small id="goal-currency-hint">Валюта заблокирована после первого immutable-вклада.</small> : null}</label>
        <label><span>Целевая сумма</span><input aria-describedby={errors.targetAmount ? "goal-amount-error" : undefined} inputMode="decimal" onChange={(event) => onChange({ ...form, targetAmount: event.target.value })} placeholder="0.00" required value={form.targetAmount}/>{errors.targetAmount ? <small className="goals-field-error" id="goal-amount-error">{errors.targetAmount}</small> : null}</label>
        <label><span>Целевая дата</span><input aria-describedby={errors.targetDate ? "goal-date-error" : undefined} onChange={(event) => onChange({ ...form, targetDate: event.target.value })} type="date" value={form.targetDate}/>{errors.targetDate ? <small className="goals-field-error" id="goal-date-error">{errors.targetDate}</small> : <small>Необязательно. Просрочку определяет backend.</small>}</label>
      </div>
      <footer><button className="secondary-button" disabled={isSaving} onClick={onClose} type="button">Отмена</button><button className="primary-button" disabled={isSaving} type="submit">{isSaving ? "Сохраняем…" : editing ? "Сохранить" : "Создать цель"}</button></footer>
    </form>
  </EntityDrawer>;
}

function ContributionDialog({
  error,
  goal,
  isSaving,
  note,
  amount,
  onAmountChange,
  onClose,
  onNoteChange,
  onSubmit,
}: {
  error: string | null;
  goal: Goal;
  isSaving: boolean;
  note: string;
  amount: string;
  onAmountChange: (value: string) => void;
  onClose: () => void;
  onNoteChange: (value: string) => void;
  onSubmit: (event: React.FormEvent) => void;
}) {
  return <ActionDialog description="Сумму и новую backend-проекцию вернёт команда. После ответа экран отдельно загрузит актуальную цель." eyebrow="Immutable event" onClose={onClose} title={`Добавить вклад · ${goal.name}`}>
    <form className="goals-command-form" onSubmit={onSubmit}>
      {error ? <div className="notice notice--error" role="alert">{error}</div> : null}
      <label><span>Сумма · {goal.currency}</span><input inputMode="decimal" onChange={(event) => onAmountChange(event.target.value)} placeholder="0.00" required value={amount}/></label>
      <label><span>Примечание</span><textarea maxLength={1000} onChange={(event) => onNoteChange(event.target.value)} rows={3} value={note}/></label>
      <small>Дата события будет текущей и timezone-aware на backend.</small>
      <footer><button className="secondary-button" disabled={isSaving} onClick={onClose} type="button">Отмена</button><button className="primary-button" disabled={isSaving} type="submit">{isSaving ? "Добавляем…" : error ? "Повторить команду" : "Добавить вклад"}</button></footer>
    </form>
  </ActionDialog>;
}

function CorrectionDialog({
  adjustment,
  contribution,
  error,
  goal,
  isSaving,
  note,
  onAdjustmentChange,
  onClose,
  onNoteChange,
  onSubmit,
}: {
  adjustment: string;
  contribution: GoalContribution;
  error: string | null;
  goal: Goal;
  isSaving: boolean;
  note: string;
  onAdjustmentChange: (value: string) => void;
  onClose: () => void;
  onNoteChange: (value: string) => void;
  onSubmit: (event: React.FormEvent) => void;
}) {
  return <ActionDialog description="Создаётся новое correction-событие. Исходный вклад не редактируется и не удаляется." eyebrow="Immutable correction" onClose={onClose} title="Исправить вклад">
    <form className="goals-command-form" onSubmit={onSubmit}>
      {error ? <div className="notice notice--error" role="alert">{error}</div> : null}
      <div className="goals-correction-source"><span>Исходное событие</span><strong>{formatGoalMoney(contribution.amount, goal.currency)}</strong></div>
      <label><span>Корректировка · {goal.currency}</span><input inputMode="decimal" onChange={(event) => onAdjustmentChange(event.target.value)} placeholder="-100.00 или 100.00" required value={adjustment}/><small>Положительная сумма увеличивает вклад, отрицательная уменьшает.</small></label>
      <label><span>Причина</span><textarea maxLength={1000} onChange={(event) => onNoteChange(event.target.value)} rows={3} value={note}/></label>
      <footer><button className="secondary-button" disabled={isSaving} onClick={onClose} type="button">Отмена</button><button className="primary-button" disabled={isSaving} type="submit">{isSaving ? "Сохраняем…" : error ? "Повторить команду" : "Создать исправление"}</button></footer>
    </form>
  </ActionDialog>;
}

export function GoalsScreen({ onError, preferredCurrency }: GoalsScreenProps) {
  const [items, setItems] = useState<Goal[]>([]);
  const [page, setPage] = useState({ limit: PAGE_LIMIT, offset: 0, total: 0 });
  const [role, setRole] = useState<string>("viewer");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<GoalStatus | "">("");
  const [currency, setCurrency] = useState("");
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [offset, setOffset] = useState(0);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState<MutationScope | "history" | null>(null);

  const [selectedGoal, setSelectedGoal] = useState<Goal | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [history, setHistory] = useState<GoalContribution[]>([]);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyError, setHistoryError] = useState<string | null>(null);

  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<Goal | null>(null);
  const [form, setForm] = useState<GoalFormState>(() => initialGoalForm(preferredCurrency));
  const [formErrors, setFormErrors] = useState<GoalFormErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [contributionGoal, setContributionGoal] = useState<Goal | null>(null);
  const [contributionAmount, setContributionAmount] = useState("");
  const [contributionNote, setContributionNote] = useState("");
  const [contributionError, setContributionError] = useState<string | null>(null);
  const [correction, setCorrection] = useState<{ goal: Goal; contribution: GoalContribution } | null>(null);
  const [correctionAmount, setCorrectionAmount] = useState("");
  const [correctionNote, setCorrectionNote] = useState("");
  const [correctionError, setCorrectionError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Goal | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const listGeneration = useRef(0);
  const detailGeneration = useRef(0);
  const historyGeneration = useRef(0);
  const selectedGoalId = useRef<string | null>(null);
  const latestGoalById = useRef(new Map<string, Goal>());
  const visibleGoalIds = useRef(new Set<string>());
  const identities = useRef<Record<MutationScope, GoalMutationIdentity>>({
    cancel: new GoalMutationIdentity(),
    complete: new GoalMutationIdentity(),
    contribution: new GoalMutationIdentity(),
    correction: new GoalMutationIdentity(),
    create: new GoalMutationIdentity(),
    delete: new GoalMutationIdentity(),
    pause: new GoalMutationIdentity(),
    reopen: new GoalMutationIdentity(),
    restore: new GoalMutationIdentity(),
    resume: new GoalMutationIdentity(),
    update: new GoalMutationIdentity(),
  });

  const canMutate = role === "editor" || role === "owner";
  const hasActiveFilters = Boolean(search.trim() || status || currency || includeDeleted);
  const pageEnd = Math.min(page.offset + items.length, page.total);

  const matchesCurrentFilters = useCallback((goal: Goal) => {
    if (!includeDeleted && goal.deleted_at) return false;
    if (status && goal.status !== status) return false;
    if (currency && goal.currency !== currency) return false;
    const normalizedSearch = search.trim().toLocaleLowerCase("ru-RU");
    if (!normalizedSearch) return true;
    return goal.name.toLocaleLowerCase("ru-RU").includes(normalizedSearch)
      || (goal.description ?? "").toLocaleLowerCase("ru-RU").includes(normalizedSearch);
  }, [currency, includeDeleted, search, status]);

  const rememberGoal = useCallback((next: Goal): Goal => {
    const cached = latestGoalById.current.get(next.id);
    if (cached && cached.version > next.version) return cached;
    latestGoalById.current.set(next.id, next);
    if (!visibleGoalIds.current.has(next.id)) return next;
    if (matchesCurrentFilters(next)) {
      setItems((current) => current.map((item) => item.id === next.id ? next : item));
    } else {
      visibleGoalIds.current.delete(next.id);
      setItems((current) => current.filter((item) => item.id !== next.id));
      setPage((current) => ({ ...current, total: Math.max(0, current.total - 1) }));
    }
    return next;
  }, [matchesCurrentFilters]);

  const loadGoals = useCallback(async (afterConfirmedMutation = false) => {
    const generation = ++listGeneration.current;
    setLoading(true);
    setLoadError(null);
    try {
      const [result, me] = await Promise.all([
        listGoals({
          currency: currency || undefined,
          includeDeleted,
          limit: PAGE_LIMIT,
          offset,
          search: search.trim() || undefined,
          status: status || undefined,
        }),
        apiClient.get<GoalAuthMeResponse>("/api/v1/auth/me"),
      ]);
      if (generation !== listGeneration.current) return;
      let omitted = 0;
      const merged = result.items.flatMap((item) => {
        const cached = latestGoalById.current.get(item.id);
        if (cached && cached.version > item.version) {
          if (matchesCurrentFilters(cached)) return [cached];
          omitted += 1;
          return [];
        }
        latestGoalById.current.set(item.id, item);
        return [item];
      });
      visibleGoalIds.current = new Set(merged.map((item) => item.id));
      setItems(merged);
      setPage(omitted
        ? { ...result.page, total: Math.max(result.page.offset + merged.length, result.page.total - omitted) }
        : result.page);
      setRole(me.role);
    } catch (error) {
      if (generation !== listGeneration.current) return;
      setLoadError(error instanceof ApiClientError ? goalErrorMessage(error) : "Не удалось загрузить цели.");
      if (afterConfirmedMutation) {
        setNotice({ text: CONFIRMED_REFRESH_WARNING, tone: "warning" });
      }
      onError(error);
    } finally {
      if (generation === listGeneration.current) setLoading(false);
    }
  }, [currency, includeDeleted, matchesCurrentFilters, offset, onError, search, status]);

  useEffect(() => { queueMicrotask(() => void loadGoals()); }, [loadGoals]);

  const loadHistory = useCallback(async (goalId: string, nextOffset: number, replace: boolean) => {
    const generation = historyGeneration.current;
    setBusy("history");
    setHistoryError(null);
    try {
      const result = await listGoalContributions(goalId, nextOffset, HISTORY_LIMIT, true);
      if (generation !== historyGeneration.current || selectedGoalId.current !== goalId) return;
      setHistory((current) => {
        const combined = replace ? result.items : [...current, ...result.items];
        const seen = new Set<string>();
        return combined.filter((entry) => {
          if (seen.has(entry.id)) return false;
          seen.add(entry.id);
          return true;
        });
      });
      setHistoryOffset(nextOffset + result.items.length);
      setHistoryTotal(result.page.total);
    } catch (error) {
      if (generation !== historyGeneration.current || selectedGoalId.current !== goalId) return;
      setHistoryError(goalErrorMessage(error));
    } finally {
      if (generation === historyGeneration.current && selectedGoalId.current === goalId) setBusy(null);
    }
  }, []);

  async function openGoal(goal: Goal) {
    for (const scope of ["pause", "resume", "complete", "reopen", "cancel", "restore"] as const) {
      resetIdentity(scope);
    }
    const goalId = goal.id;
    setBusy((current) => current === "history" ? null : current);
    const generation = ++detailGeneration.current;
    historyGeneration.current += 1;
    selectedGoalId.current = goalId;
    setSelectedGoal(goal);
    setDetailOpen(true);
    setDetailLoading(true);
    setDetailError(null);
    setHistory([]);
    setHistoryOffset(0);
    setHistoryTotal(0);
    setHistoryError(null);
    try {
      const fresh = await getGoal(goalId, true);
      if (generation !== detailGeneration.current || selectedGoalId.current !== goalId) return;
      setSelectedGoal(rememberGoal(fresh));
      await loadHistory(goalId, 0, true);
    } catch (error) {
      if (generation !== detailGeneration.current || selectedGoalId.current !== goalId) return;
      setDetailError(goalErrorMessage(error));
    } finally {
      if (generation === detailGeneration.current && selectedGoalId.current === goalId) setDetailLoading(false);
    }
  }

  function closeDetail() {
    if (busy && busy !== "history") return;
    discardDetail();
  }

  function discardDetail() {
    detailGeneration.current += 1;
    historyGeneration.current += 1;
    selectedGoalId.current = null;
    setBusy((current) => current === "history" ? null : current);
    setDetailOpen(false);
    setDetailLoading(false);
    setDetailError(null);
    setSelectedGoal(null);
    setHistory([]);
    setHistoryOffset(0);
    setHistoryTotal(0);
    setHistoryError(null);
  }

  async function converge(goalId: string, reloadHistory = false, afterConfirmedMutation = false) {
    const fresh = await getGoal(goalId, true);
    const effective = rememberGoal(fresh);
    if (selectedGoalId.current === goalId) setSelectedGoal(effective);
    await loadGoals(afterConfirmedMutation);
    if (reloadHistory && selectedGoalId.current === goalId) {
      historyGeneration.current += 1;
      setHistory([]);
      setHistoryOffset(0);
      setHistoryTotal(0);
      await loadHistory(goalId, 0, true);
    }
    return fresh;
  }

  function resetIdentity(scope: MutationScope) {
    identities.current[scope].reset();
  }

  function closeMutationSurfaces() {
    setEditorOpen(false);
    setEditing(null);
    setContributionGoal(null);
    setCorrection(null);
    setDeleteTarget(null);
  }

  async function refreshRejectedGoal(scope: MutationScope, goalId: string | undefined, message: string) {
    resetIdentity(scope);
    closeMutationSurfaces();
    setNotice({ text: message, tone: "warning" });
    if (!goalId) {
      await loadGoals();
      return;
    }
    try {
      await converge(goalId, true);
    } catch (refreshError) {
      if (refreshError instanceof ApiClientError && refreshError.code === "GOAL_NOT_FOUND") {
        if (selectedGoalId.current === goalId) discardDetail();
      } else if (selectedGoalId.current === goalId) {
        setDetailError("Не удалось загрузить актуальную цель. Закройте и откройте её повторно.");
      }
      await loadGoals();
    }
  }

  async function handleVersionConflict(scope: MutationScope, goalId?: string) {
    await refreshRejectedGoal(
      scope,
      goalId,
      "Цель изменилась в другой сессии. Актуальная версия запрошена; старая команда не повторялась.",
    );
  }

  async function handleRejectedGoal(scope: MutationScope, goalId: string, error: ApiClientError) {
    await refreshRejectedGoal(scope, goalId, goalErrorMessage(error));
  }

  async function refreshAfterConfirmedSuccess(goalId: string, reloadHistory = false) {
    try {
      await converge(goalId, reloadHistory, true);
    } catch {
      setNotice({ text: CONFIRMED_REFRESH_WARNING, tone: "warning" });
      if (selectedGoalId.current === goalId) setDetailError(CONFIRMED_REFRESH_WARNING);
      await loadGoals(true);
    }
  }

  function changeFilters(change: () => void) {
    listGeneration.current += 1;
    setOffset(0);
    change();
  }

  function openCreate() {
    setEditing(null);
    setForm(initialGoalForm(preferredCurrency));
    setFormErrors({});
    setFormError(null);
    resetIdentity("create");
    setEditorOpen(true);
  }

  function openEdit(goal: Goal) {
    setEditing(goal);
    setForm(goalFormFromRecord(goal));
    setFormErrors({});
    setFormError(null);
    resetIdentity("update");
    setEditorOpen(true);
  }

  function changeGoalForm(next: GoalFormState) {
    resetIdentity(editing ? "update" : "create");
    setForm(next);
    setFormErrors({});
    setFormError(null);
  }

  function closeEditor() {
    if (busy === "create" || busy === "update") return;
    resetIdentity(editing ? "update" : "create");
    setEditorOpen(false);
    setEditing(null);
    setFormError(null);
  }

  async function saveGoal(event: React.FormEvent) {
    event.preventDefault();
    const errors = validateGoalForm(form);
    setFormErrors(errors);
    if (Object.keys(errors).length) return;
    const scope: MutationScope = editing ? "update" : "create";
    setBusy(scope);
    setFormError(null);
    try {
      const result = editing
        ? await updateGoal(editing.id, goalUpdateRequest(form, editing.version), identities.current.update.current())
        : await createGoal(goalCreateRequest(form), identities.current.create.current());
      resetIdentity(scope);
      setEditorOpen(false);
      setEditing(null);
      setNotice({ text: editing ? "Цель обновлена." : "Цель создана.", tone: "success" });
      const effective = rememberGoal(result);
      if (selectedGoalId.current === result.id) setSelectedGoal(effective);
      await loadGoals(true);
    } catch (error) {
      if (error instanceof ApiClientError && error.code === "GOAL_VERSION_CONFLICT") {
        await handleVersionConflict(scope, editing?.id);
      } else if (editing && error instanceof ApiClientError && refreshRequiredCodes.has(error.code)) {
        await handleRejectedGoal(scope, editing.id, error);
      } else {
        if (!isUncertainGoalError(error)) resetIdentity(scope);
        setFormError(goalErrorMessage(error));
      }
    } finally {
      setBusy(null);
    }
  }

  function openContribution(goal: Goal) {
    resetIdentity("contribution");
    setContributionGoal(goal);
    setContributionAmount("");
    setContributionNote("");
    setContributionError(null);
  }

  async function submitContribution(event: React.FormEvent) {
    event.preventDefault();
    if (!contributionGoal) return;
    const amount = normalizeGoalMoney(contributionAmount);
    if (!/^\d+(?:\.\d{1,4})?$/.test(amount) || Number(amount) <= 0) {
      setContributionError("Введите положительную сумму с точностью до 4 знаков.");
      return;
    }
    setBusy("contribution");
    setContributionError(null);
    try {
      const command = await addGoalContribution(contributionGoal.id, { amount, note: contributionNote.trim() || null }, identities.current.contribution.current());
      const effective = rememberGoal(command.goal);
      setSelectedGoal((current) => current?.id === effective.id ? effective : current);
      resetIdentity("contribution");
      setContributionGoal(null);
      setNotice({ text: `Вклад ${formatGoalMoney(command.contribution.amount, command.goal.currency)} принят. Актуальная проекция загружена отдельно.`, tone: "success" });
      await refreshAfterConfirmedSuccess(command.goal.id, true);
    } catch (error) {
      if (error instanceof ApiClientError && error.code === "GOAL_VERSION_CONFLICT") {
        await handleVersionConflict("contribution", contributionGoal.id);
      } else if (error instanceof ApiClientError && refreshRequiredCodes.has(error.code)) {
        await handleRejectedGoal("contribution", contributionGoal.id, error);
      } else {
        if (!isUncertainGoalError(error)) resetIdentity("contribution");
        setContributionError(goalErrorMessage(error));
      }
    } finally {
      setBusy(null);
    }
  }

  function openCorrection(goal: Goal, contribution: GoalContribution) {
    resetIdentity("correction");
    setCorrection({ contribution, goal });
    setCorrectionAmount("");
    setCorrectionNote("");
    setCorrectionError(null);
  }

  async function submitCorrection(event: React.FormEvent) {
    event.preventDefault();
    if (!correction) return;
    const adjustment = normalizeGoalMoney(correctionAmount);
    if (!/^-?\d+(?:\.\d{1,4})?$/.test(adjustment) || Number(adjustment) === 0) {
      setCorrectionError("Введите ненулевую положительную или отрицательную корректировку.");
      return;
    }
    setBusy("correction");
    setCorrectionError(null);
    try {
      const command = await correctGoalContribution(
        correction.goal.id,
        correction.contribution.id,
        { adjustment_amount: adjustment, note: correctionNote.trim() || null },
        identities.current.correction.current(),
      );
      const effective = rememberGoal(command.goal);
      setSelectedGoal((current) => current?.id === effective.id ? effective : current);
      resetIdentity("correction");
      setCorrection(null);
      setNotice({ text: `Correction-событие ${formatGoalMoney(command.contribution.amount, command.goal.currency)} создано.`, tone: "success" });
      await refreshAfterConfirmedSuccess(command.goal.id, true);
    } catch (error) {
      if (error instanceof ApiClientError && error.code === "GOAL_VERSION_CONFLICT") {
        await handleVersionConflict("correction", correction.goal.id);
      } else if (error instanceof ApiClientError && refreshRequiredCodes.has(error.code)) {
        await handleRejectedGoal("correction", correction.goal.id, error);
      } else {
        if (!isUncertainGoalError(error)) resetIdentity("correction");
        setCorrectionError(goalErrorMessage(error));
      }
    } finally {
      setBusy(null);
    }
  }

  async function performLifecycle(goal: Goal, operation: LifecycleOperation) {
    setBusy(operation);
    try {
      const result = await runGoalLifecycle(goal.id, operation, goal.version, identities.current[operation].current());
      const effective = rememberGoal(result);
      setSelectedGoal((current) => current?.id === effective.id ? effective : current);
      resetIdentity(operation);
      setNotice({ text: `Команда «${lifecycleLabels[operation]}» выполнена.`, tone: "success" });
      await refreshAfterConfirmedSuccess(result.id);
    } catch (error) {
      if (error instanceof ApiClientError && error.code === "GOAL_VERSION_CONFLICT") {
        await handleVersionConflict(operation, goal.id);
      } else if (error instanceof ApiClientError && refreshRequiredCodes.has(error.code)) {
        await handleRejectedGoal(operation, goal.id, error);
      } else {
        if (!isUncertainGoalError(error)) resetIdentity(operation);
        setNotice({ text: goalErrorMessage(error), tone: "error" });
      }
    } finally {
      setBusy(null);
    }
  }

  async function performDelete() {
    if (!deleteTarget) return;
    setBusy("delete");
    setDeleteError(null);
    try {
      const result = await deleteGoal(deleteTarget.id, deleteTarget.version, identities.current.delete.current());
      const effective = rememberGoal(result);
      setSelectedGoal((current) => current?.id === effective.id ? effective : current);
      resetIdentity("delete");
      setDeleteTarget(null);
      setNotice({ text: "Цель удалена мягко. Lifecycle и история вкладов сохранены.", tone: "success" });
      await refreshAfterConfirmedSuccess(result.id, true);
    } catch (error) {
      if (error instanceof ApiClientError && error.code === "GOAL_VERSION_CONFLICT") {
        await handleVersionConflict("delete", deleteTarget.id);
      } else if (error instanceof ApiClientError && refreshRequiredCodes.has(error.code)) {
        await handleRejectedGoal("delete", deleteTarget.id, error);
      } else {
        if (!isUncertainGoalError(error)) resetIdentity("delete");
        setDeleteError(goalErrorMessage(error));
      }
    } finally {
      setBusy(null);
    }
  }

  async function performRestore(goal: Goal) {
    setBusy("restore");
    try {
      const result = await restoreGoal(goal.id, goal.version, identities.current.restore.current());
      const effective = rememberGoal(result);
      setSelectedGoal((current) => current?.id === effective.id ? effective : current);
      resetIdentity("restore");
      setNotice({ text: `Цель восстановлена со статусом «${goalStatusLabels[result.status]}».`, tone: "success" });
      await refreshAfterConfirmedSuccess(result.id, true);
    } catch (error) {
      if (error instanceof ApiClientError && error.code === "GOAL_VERSION_CONFLICT") {
        await handleVersionConflict("restore", goal.id);
      } else if (error instanceof ApiClientError && refreshRequiredCodes.has(error.code)) {
        await handleRejectedGoal("restore", goal.id, error);
      } else {
        if (!isUncertainGoalError(error)) resetIdentity("restore");
        setNotice({ text: goalErrorMessage(error), tone: "error" });
      }
    } finally {
      setBusy(null);
    }
  }

  const totalPages = Math.max(1, Math.ceil(page.total / PAGE_LIMIT));
  const currentPage = Math.floor(page.offset / PAGE_LIMIT) + 1;

  return <section className="goals-screen">
    <header className="screen-header goals-screen-header"><div><span className="kicker">Планирование накоплений</span><h1>Цели</h1><p>Прогресс и остаток — backend-проекция immutable-вкладов. Валюты не смешиваются.</p></div><div className="screen-header-actions"><button className="secondary-button" disabled={loading} onClick={() => void loadGoals()} type="button">Обновить</button>{canMutate ? <button className="primary-button" onClick={openCreate} type="button">＋ Создать цель</button> : null}</div></header>

    <section aria-label="Фильтры целей" className="panel goals-toolbar">
      <label className="goals-search"><span>Поиск</span><input onChange={(event) => changeFilters(() => setSearch(event.target.value))} placeholder="Название или описание" type="search" value={search}/></label>
      <label><span>Статус</span><select onChange={(event) => changeFilters(() => setStatus(event.target.value as GoalStatus | ""))} value={status}><option value="">Все статусы</option>{Object.entries(goalStatusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label><span>Валюта</span><input maxLength={3} onChange={(event) => changeFilters(() => setCurrency(event.target.value.toUpperCase().replace(/[^A-Z]/g, "")))} placeholder="Все" value={currency}/></label>
      <label className="goals-deleted-toggle"><input checked={includeDeleted} onChange={(event) => changeFilters(() => setIncludeDeleted(event.target.checked))} type="checkbox"/><span>Показывать удалённые</span></label>
      <button className="text-button" onClick={() => changeFilters(() => { setSearch(""); setStatus(""); setCurrency(""); setIncludeDeleted(false); })} type="button">Сбросить</button>
    </section>

    {notice ? <div className={`notice notice--${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}><span>{notice.text}</span><button aria-label="Закрыть уведомление" className="text-button" onClick={() => setNotice(null)} type="button">×</button></div> : null}
    {loading ? <div aria-label="Загружаем цели" aria-live="polite" className="goals-loading"><i/><i/><i/><i/></div> : null}
    {!loading && loadError ? <section className="panel goals-load-error" role="alert"><strong>Цели не загрузились</strong><span>{loadError}</span><button className="secondary-button" onClick={() => void loadGoals()} type="button">Повторить</button></section> : null}

    {!loading && !loadError && !items.length ? <section className="panel goals-empty"><div className="goals-empty-symbol">◎</div><span className="kicker">Только реальные данные</span><h2>{hasActiveFilters ? "По выбранным фильтрам целей нет" : "Целей пока нет"}</h2><p>{canMutate ? "Создайте первую цель — начальный статус и все финансовые показатели назначит backend." : "В этом пространстве пока нет доступных целей. Роль viewer работает только на чтение."}</p>{canMutate && !hasActiveFilters ? <button className="primary-button" onClick={openCreate} type="button">Создать цель</button> : null}</section> : null}

    {!loading && !loadError && items.length ? <>
      <div className="goals-result-meta"><span>Найдено: {page.total}</span><small>Показаны {page.offset + 1}–{pageEnd}. Суммы не объединяются между валютами.</small></div>
      <section aria-label="Список целей" className="goals-grid">{items.map((goal) => <GoalCard goal={goal} key={goal.id} onOpen={(item) => void openGoal(item)}/>)}</section>
      {page.total > PAGE_LIMIT ? <nav aria-label="Страницы целей" className="goals-pagination"><button className="secondary-button" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - PAGE_LIMIT))} type="button">← Назад</button><span>Страница {currentPage} из {totalPages}</span><button className="secondary-button" disabled={offset + items.length >= page.total || loading} onClick={() => setOffset(offset + items.length)} type="button">Далее →</button></nav> : null}
    </> : null}

    {detailOpen ? <EntityDrawer ariaLabel="Детали цели" eyebrow="Backend projection" onClose={closeDetail} subtitle={selectedGoal ? `${selectedGoal.currency} · версия ${selectedGoal.version}` : "Загрузка"} title={selectedGoal?.name ?? "Цель"}>
      {detailLoading ? <div className="goals-detail-loading" aria-live="polite">Загружаем актуальную цель…</div> : null}
      {detailError ? <div className="notice notice--error" role="alert">{detailError}</div> : null}
      {selectedGoal ? <div className="goals-detail">
        <div className="goals-detail-status"><span className={`goals-status goals-status--${selectedGoal.status}`}>{goalStatusLabels[selectedGoal.status]}</span>{selectedGoal.deleted_at ? <span className="goals-deleted-badge">Удалена</span> : null}<span>{goalDeadlineLabel(selectedGoal)}</span></div>
        {selectedGoal.description ? <p className="goals-detail-description">{selectedGoal.description}</p> : null}
        <GoalStateNotes goal={selectedGoal}/>
        <GoalProgress goal={selectedGoal}/>
        <dl className="goals-detail-money"><div><dt>Целевая сумма</dt><dd>{formatGoalMoney(selectedGoal.target_amount, selectedGoal.currency)}</dd></div><div><dt>Внесено</dt><dd>{formatGoalMoney(selectedGoal.contributed_amount, selectedGoal.currency)}</dd></div><div><dt>Осталось</dt><dd>{formatGoalMoney(selectedGoal.remaining_amount, selectedGoal.currency)}</dd></div><div><dt>Событий</dt><dd>{selectedGoal.contribution_count}</dd></div></dl>

        {canMutate ? <section className="goals-actions" aria-label="Команды цели">
          {!selectedGoal.deleted_at && ["active", "paused"].includes(selectedGoal.status) ? <button className="secondary-button" disabled={busy !== null} onClick={() => openEdit(selectedGoal)} type="button">Изменить</button> : null}
          {!selectedGoal.deleted_at && selectedGoal.status === "active" ? <button className="primary-button" disabled={busy !== null} onClick={() => openContribution(selectedGoal)} type="button">Добавить вклад</button> : null}
          {!selectedGoal.deleted_at && selectedGoal.status === "active" ? <button className="secondary-button" disabled={busy !== null} onClick={() => void performLifecycle(selectedGoal, "pause")} type="button">Приостановить</button> : null}
          {!selectedGoal.deleted_at && selectedGoal.status === "paused" ? <button className="secondary-button" disabled={busy !== null} onClick={() => void performLifecycle(selectedGoal, "resume")} type="button">Возобновить</button> : null}
          {!selectedGoal.deleted_at && ["active", "paused"].includes(selectedGoal.status) && selectedGoal.is_target_reached ? <button className="secondary-button" disabled={busy !== null} onClick={() => void performLifecycle(selectedGoal, "complete")} type="button">Завершить</button> : null}
          {!selectedGoal.deleted_at && selectedGoal.status === "completed" ? <button className="secondary-button" disabled={busy !== null} onClick={() => void performLifecycle(selectedGoal, "reopen")} type="button">Вернуть в активные</button> : null}
          {!selectedGoal.deleted_at && ["active", "paused"].includes(selectedGoal.status) ? <button className="secondary-button" disabled={busy !== null} onClick={() => void performLifecycle(selectedGoal, "cancel")} type="button">Отменить цель</button> : null}
          {!selectedGoal.deleted_at ? <button className="danger-button" disabled={busy !== null} onClick={() => { resetIdentity("delete"); setDeleteError(null); setDeleteTarget(selectedGoal); }} type="button">Удалить</button> : <button className="primary-button" disabled={busy !== null} onClick={() => void performRestore(selectedGoal)} type="button">Восстановить · {goalStatusLabels[selectedGoal.status]}</button>}
        </section> : <div className="goals-viewer-note">Viewer · просмотр цели и истории без команд изменения.</div>}

        <section className="goals-history" aria-labelledby="goals-history-title"><div className="goals-history-heading"><div><span className="kicker">Immutable events</span><h3 id="goals-history-title">История вкладов</h3></div><span className="count-badge">{historyTotal}</span></div>
          {historyError ? <div className="notice notice--error" role="alert">{historyError}</div> : null}
          {history.map((entry) => <article className={entry.correction_of_id ? "goals-history-row goals-history-row--correction" : "goals-history-row"} key={entry.id}><div><span>{entry.correction_of_id ? "Исправление" : "Вклад"}</span><strong>{formatGoalMoney(entry.amount, entry.currency)}</strong></div><div><span>{new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(new Date(entry.contributed_at))}</span><small>{entry.created_by_display_name ?? entry.created_by}</small>{entry.note ? <p>{entry.note}</p> : null}{entry.correction_of_id ? <small>Корректирует событие {entry.correction_of_id}</small> : null}</div>{canMutate && !selectedGoal.deleted_at && entry.correction_of_id === null ? <button className="text-button" disabled={busy !== null} onClick={() => openCorrection(selectedGoal, entry)} type="button">Исправить</button> : null}</article>)}
          {!history.length && busy !== "history" ? <div className="goals-history-empty">Событий вкладов пока нет.</div> : null}
          {busy === "history" ? <div aria-live="polite" className="goals-history-loading">Загружаем историю…</div> : null}
          {historyOffset < historyTotal ? <button className="secondary-button" disabled={busy !== null} onClick={() => void loadHistory(selectedGoal.id, historyOffset, false)} type="button">Загрузить ещё</button> : null}
        </section>
      </div> : null}
    </EntityDrawer> : null}

    {editorOpen ? <GoalEditor editing={editing} errors={formErrors} form={form} formError={formError} isSaving={busy === "create" || busy === "update"} onChange={changeGoalForm} onClose={closeEditor} onSave={(event) => void saveGoal(event)}/> : null}
    {contributionGoal ? <ContributionDialog amount={contributionAmount} error={contributionError} goal={contributionGoal} isSaving={busy === "contribution"} note={contributionNote} onAmountChange={(value) => { resetIdentity("contribution"); setContributionAmount(value); setContributionError(null); }} onClose={() => { if (busy !== "contribution") { resetIdentity("contribution"); setContributionGoal(null); } }} onNoteChange={(value) => { resetIdentity("contribution"); setContributionNote(value); setContributionError(null); }} onSubmit={(event) => void submitContribution(event)}/> : null}
    {correction ? <CorrectionDialog adjustment={correctionAmount} contribution={correction.contribution} error={correctionError} goal={correction.goal} isSaving={busy === "correction"} note={correctionNote} onAdjustmentChange={(value) => { resetIdentity("correction"); setCorrectionAmount(value); setCorrectionError(null); }} onClose={() => { if (busy !== "correction") { resetIdentity("correction"); setCorrection(null); } }} onNoteChange={(value) => { resetIdentity("correction"); setCorrectionNote(value); setCorrectionError(null); }} onSubmit={(event) => void submitCorrection(event)}/> : null}
    {deleteTarget ? <ActionDialog description="Это soft delete. Lifecycle, вклад и correction-события сохранятся. Восстановление вернёт тот же lifecycle status." eyebrow="Destructive command" onClose={() => { if (busy !== "delete") { resetIdentity("delete"); setDeleteTarget(null); setDeleteError(null); } }} title={`Удалить цель «${deleteTarget.name}»?`}>{deleteError ? <div className="notice notice--error" role="alert">{deleteError}</div> : null}<div className="goals-delete-warning"><strong>Версия {deleteTarget.version} · {goalStatusLabels[deleteTarget.status]}</strong><span>Удаление не означает отмену и не изменяет финансовые события.</span></div><footer><button className="secondary-button" disabled={busy === "delete"} onClick={() => { resetIdentity("delete"); setDeleteTarget(null); setDeleteError(null); }} type="button">Отмена</button><button className="danger-button" disabled={busy === "delete"} onClick={() => void performDelete()} type="button">{busy === "delete" ? "Удаляем…" : deleteError ? "Повторить команду" : "Удалить цель"}</button></footer></ActionDialog> : null}
  </section>;
}
