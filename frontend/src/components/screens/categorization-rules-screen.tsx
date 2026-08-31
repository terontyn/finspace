"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { EntityDrawer } from "@/components/ui/entity-drawer";
import {
  categorizationFormHasMatcher,
  categorizationRuleFormFromRecord,
  categorizationRuleMutation,
  categorizationUiError,
  initialCategorizationRuleForm,
  type CategorizationRuleForm,
  type CategorizationUiError,
} from "@/lib/categorization";
import { apiClient, type WorkspaceRole } from "@/lib/api-client";
import type { CategorizationRule, CategorizationRulePage } from "@/types/categorization";
import type { Account, Category, Paged, Payee } from "@/types/finance";

interface CategorizationRulesScreenProps {
  onError: (error: unknown) => void;
  role: WorkspaceRole | null;
  roleLoading: boolean;
}

interface CanonicalEntities {
  accounts: Account[];
  categories: Category[];
  payees: Payee[];
}

const limit = 12;
const transactionTypeLabels = {
  adjustment: "Корректировка",
  expense: "Расход",
  income: "Доход",
  refund: "Возврат",
} as const;

function entityName(items: Array<{ id: string; name: string }>, id: string | null, fallback: string): string | null {
  if (!id) return null;
  return items.find((item) => item.id === id)?.name ?? fallback;
}

function ruleConditions(rule: CategorizationRule, entities: CanonicalEntities): string[] {
  const conditions: string[] = [];
  if (rule.transaction_type) conditions.push(`Тип: ${transactionTypeLabels[rule.transaction_type]}`);
  const account = entityName(entities.accounts, rule.account_id, "Недоступный счёт");
  if (account) conditions.push(`Счёт: ${account}`);
  const payee = entityName(entities.payees, rule.payee_id, "Недоступный получатель");
  if (payee) conditions.push(`Получатель: ${payee}`);
  if (rule.counterparty_contains) conditions.push(`Контрагент содержит «${rule.counterparty_contains}»`);
  if (rule.description_contains) conditions.push(`Описание содержит «${rule.description_contains}»`);
  return conditions;
}

function InlineError({ error }: { error: CategorizationUiError }) {
  return <div className="categorization-inline-error" role="alert"><strong>{error.message}</strong><code>{error.code}</code>{error.requestId ? <small>Запрос {error.requestId}</small> : null}</div>;
}

function RuleDrawer({ editing, entities, error, form, isSaving, onChange, onClose, onSave }: {
  editing: CategorizationRule | null;
  entities: CanonicalEntities;
  error: CategorizationUiError | null;
  form: CategorizationRuleForm;
  isSaving: boolean;
  onChange: (form: CategorizationRuleForm) => void;
  onClose: () => void;
  onSave: (event: React.FormEvent) => void;
}) {
  const availableAccounts = entities.accounts.filter((account) => !account.is_archived);
  const availablePayees = entities.payees.filter((payee) => !payee.deleted_at);
  const availableCategories = entities.categories.filter((category) => !category.is_archived);
  // A rule may reference an entity that is archived, deleted or simply outside the fetched catalog
  // page. Without an explicit option the select cannot represent the saved matcher, and an unrelated
  // edit would silently resubmit the unavailable reference.
  const missingAccount = Boolean(form.accountId) && !availableAccounts.some((account) => account.id === form.accountId);
  const missingPayee = Boolean(form.payeeId) && !availablePayees.some((payee) => payee.id === form.payeeId);
  const missingCategory = Boolean(form.categoryId) && !availableCategories.some((category) => category.id === form.categoryId);
  return <EntityDrawer ariaLabel={editing ? "Редактирование правила категоризации" : "Новое правило категоризации"} eyebrow={editing ? "Версионное изменение" : "Явное правило"} onClose={onClose} subtitle={editing ? `Версия ${editing.version}` : "Backend проверит ссылки и условия"} title={editing?.name ?? "Создать правило"}>
    <form className="categorization-rule-form" onSubmit={onSave}>
      <label><span>Название</span><input autoComplete="off" maxLength={200} onChange={(event) => onChange({ ...form, name: event.target.value })} required value={form.name}/></label>
      <div className="categorization-form-grid">
        <label><span>Приоритет</span><input inputMode="numeric" min={0} onChange={(event) => onChange({ ...form, priority: event.target.value })} required type="number" value={form.priority}/><small>Меньшее число проверяется раньше.</small></label>
        <label className="categorization-active-toggle"><input checked={form.isActive} onChange={(event) => onChange({ ...form, isActive: event.target.checked })} type="checkbox"/><span>Правило активно</span></label>
      </div>
      <section className="categorization-matchers" aria-label="Условия правила">
        <div><span className="kicker">Условия совпадения</span><strong>Все заполненные условия объединяются через AND</strong><small>Получатель — каноническая запись. Исходный контрагент остаётся отдельным текстом.</small></div>
        <label><span>Тип операции</span><select onChange={(event) => onChange({ ...form, transactionType: event.target.value as CategorizationRuleForm["transactionType"] })} value={form.transactionType}><option value="">Любой поддерживаемый тип</option>{Object.entries(transactionTypeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label><span>Счёт</span><select aria-label="Счёт правила" onChange={(event) => onChange({ ...form, accountId: event.target.value })} value={form.accountId}><option value="">Любой счёт</option>{missingAccount ? <option value={form.accountId}>Текущий счёт недоступен</option> : null}{availableAccounts.map((account) => <option key={account.id} value={account.id}>{account.name} · {account.currency}</option>)}</select>{missingAccount ? <small>Счёт условия недоступен. Выберите «Любой счёт» или другой счёт, иначе backend отклонит сохранение.</small> : null}</label>
        <label><span>Получатель</span><select aria-label="Получатель правила" onChange={(event) => onChange({ ...form, payeeId: event.target.value })} value={form.payeeId}><option value="">Любой получатель</option>{missingPayee ? <option value={form.payeeId}>Текущий получатель недоступен</option> : null}{availablePayees.map((payee) => <option key={payee.id} value={payee.id}>{payee.name}</option>)}</select><small>{missingPayee ? "Получатель условия недоступен. Выберите «Любой получатель» или другого получателя, иначе backend отклонит сохранение." : "Выбор не выводится из поля «Контрагент»."}</small></label>
        <label><span>Контрагент содержит</span><input maxLength={300} onChange={(event) => onChange({ ...form, counterpartyContains: event.target.value })} placeholder="Например, COFFEE SHOP" value={form.counterpartyContains}/></label>
        <label><span>Описание содержит</span><input maxLength={300} onChange={(event) => onChange({ ...form, descriptionContains: event.target.value })} placeholder="Например, подписка" value={form.descriptionContains}/></label>
      </section>
      <label><span>Целевая категория</span><select aria-label="Целевая категория" onChange={(event) => onChange({ ...form, categoryId: event.target.value })} required value={form.categoryId}><option value="">Выберите категорию</option>{missingCategory ? <option value={form.categoryId}>Текущая категория недоступна</option> : null}{availableCategories.map((category) => <option key={category.id} value={category.id}>{category.name} · {category.category_type}</option>)}</select>{missingCategory ? <small>Целевая категория недоступна. Выберите другую категорию, иначе backend отклонит сохранение.</small> : null}</label>
      {error ? <InlineError error={error}/> : null}
      <footer><button className="secondary-button" onClick={onClose} type="button">Отмена</button><button className="primary-button" disabled={isSaving || !form.name.trim() || !form.categoryId} type="submit">{isSaving ? "Сохраняем…" : editing ? "Сохранить" : "Создать правило"}</button></footer>
    </form>
  </EntityDrawer>;
}

export function CategorizationRulesScreen({ onError, role, roleLoading }: CategorizationRulesScreenProps) {
  const [items, setItems] = useState<CategorizationRule[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [activeFilter, setActiveFilter] = useState("");
  const [entities, setEntities] = useState<CanonicalEntities>({ accounts: [], categories: [], payees: [] });
  const [isLoading, setIsLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<CategorizationRule | null>(null);
  const [form, setForm] = useState<CategorizationRuleForm>(() => initialCategorizationRuleForm());
  const [formError, setFormError] = useState<CategorizationUiError | null>(null);
  const [screenError, setScreenError] = useState<CategorizationUiError | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const canWrite = role === "owner" || role === "editor";

  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (includeDeleted) params.set("include_deleted", "true");
    if (activeFilter) params.set("is_active", activeFilter);
    return params.toString();
  }, [activeFilter, includeDeleted, offset]);

  const load = useCallback(async () => {
    let redirectedToValidPage = false;
    setIsLoading(true);
    setLoadFailed(false);
    try {
      const [rules, accounts, categories, payees] = await Promise.all([
        apiClient.get<CategorizationRulePage>(`/api/v1/categorization-rules?${query}`),
        apiClient.get<Paged<Account>>("/api/v1/accounts?is_archived=false&limit=200"),
        apiClient.get<Paged<Category>>("/api/v1/categories?is_archived=false&limit=500"),
        apiClient.get<Paged<Payee>>("/api/v1/payees?limit=200&offset=0"),
      ]);
      setEntities({ accounts: accounts.items, categories: categories.items, payees: payees.items });
      const lastOffset = rules.page.total > 0 ? Math.floor((rules.page.total - 1) / limit) * limit : 0;
      if (rules.page.offset > lastOffset) {
        redirectedToValidPage = true;
        setOffset(lastOffset);
        return;
      }
      setItems(rules.items);
      setTotal(rules.page.total);
    } catch (error) {
      setLoadFailed(true);
      onError(error);
    } finally {
      if (!redirectedToValidPage) setIsLoading(false);
    }
  }, [onError, query]);

  useEffect(() => { queueMicrotask(() => void load()); }, [load]);

  function openCreate() {
    setEditing(null);
    setForm(initialCategorizationRuleForm());
    setFormError(null);
    setDrawerOpen(true);
  }

  function startEdit(rule: CategorizationRule) {
    setEditing(rule);
    setForm(categorizationRuleFormFromRecord(rule));
    setFormError(null);
    setDrawerOpen(true);
  }

  function closeDrawer() {
    setDrawerOpen(false);
    setEditing(null);
    setForm(initialCategorizationRuleForm());
    setFormError(null);
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!canWrite) return;
    if (!categorizationFormHasMatcher(form)) {
      setFormError({ code: "CATEGORIZATION_MATCHER_REQUIRED", message: "Добавьте хотя бы одно условие правила." });
      return;
    }
    setIsSaving(true);
    setFormError(null);
    const mutation = categorizationRuleMutation(form, editing);
    try {
      if (mutation.method === "PATCH") await apiClient.patch<CategorizationRule>(mutation.path, mutation.body);
      else await apiClient.post<CategorizationRule>(mutation.path, mutation.body);
      closeDrawer();
      await load();
    } catch (error) {
      setFormError(categorizationUiError(error));
    } finally {
      setIsSaving(false);
    }
  }

  async function archive(rule: CategorizationRule) {
    if (!canWrite) return;
    setBusyId(rule.id);
    setScreenError(null);
    try {
      await apiClient.delete<CategorizationRule>(`/api/v1/categorization-rules/${rule.id}?version=${rule.version}`);
      await load();
    } catch (error) {
      setScreenError(categorizationUiError(error));
    } finally {
      setBusyId(null);
    }
  }

  async function restore(rule: CategorizationRule) {
    if (!canWrite) return;
    setBusyId(rule.id);
    setScreenError(null);
    try {
      await apiClient.post<CategorizationRule>(`/api/v1/categorization-rules/${rule.id}/restore`, { version: rule.version });
      await load();
    } catch (error) {
      setScreenError(categorizationUiError(error));
    } finally {
      setBusyId(null);
    }
  }

  return <section className="categorization-rules-screen">
    <header className="screen-header"><div><span className="kicker">Явная категоризация</span><h1>Правила</h1><p>Все заполненные условия должны совпасть. Правила с меньшим номером приоритета проверяются раньше.</p></div><div className="screen-header-actions"><button className="secondary-button" disabled={isLoading} onClick={() => void load()} type="button">Обновить</button>{canWrite ? <button className="primary-button" onClick={openCreate} type="button">＋ Создать правило</button> : null}</div></header>
    {!roleLoading && role === "viewer" ? <div className="notice notice--info" role="status"><span>Режим просмотра: правила доступны для чтения, а preview — в операциях. Изменение и применение недоступны.</span></div> : null}
    {!roleLoading && role === null ? <div className="notice notice--warning" role="status"><span>Не удалось подтвердить права записи. Управление правилами отключено.</span></div> : null}
    {screenError ? <InlineError error={screenError}/> : null}
    <section className="panel categorization-rule-directory">
      <div className="categorization-rule-toolbar">
        <label><span>Состояние</span><select aria-label="Фильтр активности правил" onChange={(event) => { setOffset(0); setActiveFilter(event.target.value); }} value={activeFilter}><option value="">Все</option><option value="true">Активные</option><option value="false">Выключенные</option></select></label>
        <label className="categorization-archive-filter"><input checked={includeDeleted} onChange={(event) => { setOffset(0); setIncludeDeleted(event.target.checked); }} type="checkbox"/><span>Показывать архив</span></label>
        {(activeFilter || includeDeleted) ? <button className="text-button" onClick={() => { setActiveFilter(""); setIncludeDeleted(false); setOffset(0); }} type="button">Сбросить</button> : null}
      </div>
      <div className="categorization-result-meta"><span>Найдено: <strong>{total}</strong></span><span>Порядок определяет backend · меньший priority раньше</span></div>
      {isLoading ? <div aria-label="Загружаем правила" className="categorization-rule-skeleton">{Array.from({ length: 4 }, (_, index) => <i key={index}/>)}</div> : loadFailed ? <div className="empty-state"><strong>Не удалось загрузить правила</strong><span>Повторите запрос.</span><button className="secondary-button" onClick={() => void load()} type="button">Повторить</button></div> : items.length ? <div className="categorization-rule-list">{items.map((rule) => {
        const category = entityName(entities.categories, rule.category_id, "Недоступная категория");
        const conditions = ruleConditions(rule, entities);
        return <article className={rule.deleted_at ? "categorization-rule-row is-archived" : "categorization-rule-row"} data-rule-id={rule.id} key={rule.id}>
          <div className="categorization-priority"><span>Приоритет</span><strong>{rule.priority}</strong></div>
          <div className="categorization-rule-main"><div><strong>{rule.name}</strong>{rule.deleted_at ? <span className="status-chip status-chip--cancelled">В архиве</span> : rule.is_active ? <span className="status-chip status-chip--confirmed">Активно</span> : <span className="status-chip">Выключено</span>}</div><div className="categorization-condition-list" aria-label={`Условия ${rule.name}`}>{conditions.map((condition) => <span key={condition}>{condition}</span>)}</div><small>Все условия одновременно · версия {rule.version}</small></div>
          <div className="categorization-target"><span>Назначить категорию</span><strong>{category}</strong></div>
          {canWrite ? <div className="categorization-rule-actions">{rule.deleted_at ? <button className="text-button" disabled={busyId === rule.id} onClick={() => void restore(rule)} type="button">Восстановить</button> : <><button className="text-button" disabled={busyId === rule.id} onClick={() => startEdit(rule)} type="button">Изменить</button><button className="text-button text-button--danger" disabled={busyId === rule.id} onClick={() => void archive(rule)} type="button">В архив</button></>}</div> : null}
        </article>;
      })}</div> : <div className="empty-state categorization-rule-empty"><span className="empty-state-icon">⌘</span><strong>Правил пока нет</strong><span>Создайте правило с одним или несколькими явными условиями.</span>{canWrite ? <button className="primary-button" onClick={openCreate} type="button">Создать правило</button> : null}</div>}
      <div className="pagination"><span>{total ? `${offset + 1}–${Math.min(offset + limit, total)} из ${total}` : "0 правил"}</span><div><button className="secondary-button" disabled={offset === 0 || isLoading} onClick={() => setOffset(Math.max(0, offset - limit))} type="button">Назад</button><button className="secondary-button" disabled={offset + limit >= total || isLoading} onClick={() => setOffset(offset + limit)} type="button">Дальше</button></div></div>
    </section>
    {drawerOpen && canWrite ? <RuleDrawer editing={editing} entities={entities} error={formError} form={form} isSaving={isSaving} onChange={setForm} onClose={closeDrawer} onSave={(event) => void save(event)}/> : null}
  </section>;
}
