'use client';

import { useEffect, useMemo, useState } from 'react';

type Screen = 'dashboard' | 'budget' | 'transactions' | 'accounts' | 'reports' | 'payees' | 'rules' | 'recurring' | 'goals' | 'import' | 'google' | 'telegram' | 'conflicts' | 'close' | 'settings';
type Theme = 'dark' | 'light';

const navGroups: { label: string; items: { id: Screen; title: string; icon: string }[] }[] = [
  { label: 'Обзор', items: [
    { id: 'dashboard', title: 'Сегодня', icon: '⌂' }, { id: 'transactions', title: 'Операции', icon: '↕' },
    { id: 'accounts', title: 'Счета', icon: '▣' }, { id: 'reports', title: 'Отчёты', icon: '⌁' },
  ]},
  { label: 'Планирование', items: [
    { id: 'budget', title: 'Бюджет', icon: '◫' }, { id: 'recurring', title: 'Регулярные', icon: '↻' },
    { id: 'goals', title: 'Цели', icon: '◎' }, { id: 'payees', title: 'Получатели', icon: '◉' }, { id: 'rules', title: 'Правила', icon: '⌘' },
  ]},
  { label: 'Интеграции', items: [
    { id: 'import', title: 'Импорт', icon: '⇣' }, { id: 'google', title: 'Google Sheets', icon: '▦' }, { id: 'telegram', title: 'Telegram', icon: '➤' },
  ]},
  { label: 'Система', items: [
    { id: 'conflicts', title: 'Конфликты', icon: '△' }, { id: 'close', title: 'Закрытие месяца', icon: '✓' }, { id: 'settings', title: 'Настройки', icon: '⚙' },
  ]},
];

const screenTitles: Record<Screen, [string, string]> = {
  dashboard: ['Сегодня', 'Вся финансовая картина — без лишнего шума.'], budget: ['Бюджет', 'Каждому рублю — понятная задача.'],
  transactions: ['Операции', 'Единый журнал движения денег.'], accounts: ['Счета', 'Остатки и сверка в одном месте.'],
  reports: ['Отчёты', 'Смотрите на деньги в динамике.'], payees: ['Получатели', 'Единая история магазинов и переводов.'],
  rules: ['Правила', 'Автоматизируйте рутинную категоризацию.'], recurring: ['Регулярные', 'Плановые платежи под контролем.'],
  goals: ['Цели', 'Планы, которые видны в цифрах.'], import: ['Импорт', 'Загрузите выписку без дублей.'],
  google: ['Google Sheets', 'Двусторонняя синхронизация и прозрачный статус.'], telegram: ['Telegram', 'Операции и уведомления прямо в чате.'],
  conflicts: ['Конфликты', 'Решайте расхождения с полным контекстом.'], close: ['Закрытие месяца', 'Зафиксируйте период уверенно.'],
  settings: ['Настройки', 'Пространство, доступы и системные параметры.'],
};

const budgetRows = [
  ['Продукты', 26000, 18420, 'Ежедневные расходы', '#8b5cf6'], ['Транспорт', 8500, 6240, 'Ежедневные расходы', '#38bdf8'],
  ['Дом', 32000, 32000, 'Обязательные', '#f59e0b'], ['Здоровье', 7000, 2860, 'Забота о себе', '#fb7185'],
  ['Кафе и рестораны', 6500, 7820, 'Образ жизни', '#f97316'], ['Развлечения', 5000, 2140, 'Образ жизни', '#22c55e'],
];

const transactionRows = [
  ['Сегодня, 14:32', 'Перекрёсток', 'Продукты', 'Сбер •• 1842', '− 2 438,60 ₽', 'expense'],
  ['Сегодня, 09:15', 'Перевод на накопления', 'Перевод', 'Сбер → Цель', '− 10 000 ₽', 'transfer'],
  ['Вчера, 18:04', 'Яндекс Go', 'Транспорт', 'Т-Банк •• 9031', '− 684 ₽', 'expense'],
  ['21 августа', 'ООО «Альтаир»', 'Зарплата', 'Сбер •• 1842', '+ 142 500 ₽', 'income'],
  ['20 августа', 'Золотое яблоко', 'Уход', 'Т-Банк •• 9031', '− 3 260 ₽', 'expense'],
  ['19 августа', 'Вкусно — и точка', 'Кафе', 'Наличные', '− 890 ₽', 'expense'],
  ['18 августа', 'МТС', 'Связь', 'Сбер •• 1842', '− 750 ₽', 'expense'],
];

function routeToScreen(): Screen {
  if (typeof window === 'undefined') return 'dashboard';
  const slug = window.location.pathname.split('/').filter(Boolean)[0] as Screen | undefined;
  return slug && Object.hasOwn(screenTitles, slug) ? slug : 'dashboard';
}

export function FinspacePrototype({ initialPath = 'dashboard' }: { initialPath?: string }) {
  const [screen, setScreen] = useState<Screen>(() => Object.hasOwn(screenTitles, initialPath) ? initialPath as Screen : 'dashboard');
  const [theme, setTheme] = useState<Theme>('dark');
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);
  const [notice, setNotice] = useState('');
  const [selected, setSelected] = useState<number[]>([]);

  useEffect(() => {
    const onPop = () => setScreen(routeToScreen());
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); setCommandOpen((value) => !value); }
      if (event.key === 'Escape') { setCommandOpen(false); setAddOpen(false); }
    };
    window.addEventListener('popstate', onPop); window.addEventListener('keydown', onKey);
    return () => { window.removeEventListener('popstate', onPop); window.removeEventListener('keydown', onKey); };
  }, []);

  const navigate = (next: Screen) => {
    setScreen(next); window.history.pushState({}, '', next === 'dashboard' ? '/' : `/${next}`);
    setSidebarOpen(false); window.scrollTo({ top: 0, behavior: 'smooth' });
  };
  const flash = (message: string) => { setNotice(message); window.setTimeout(() => setNotice(''), 2600); };

  return <div className="app-shell" data-theme={theme}>
    <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
      <div className="brand" onClick={() => navigate('dashboard')} role="button" tabIndex={0}>
        <span className="brand-mark">₽</span><span><b>Финпространство</b><small>личные финансы</small></span>
      </div>
      <nav className="nav-scroll" aria-label="Основная навигация">
        {navGroups.map((group) => <div className="nav-group" key={group.label}><p>{group.label}</p>
          {group.items.map((item) => <a key={item.id} href={item.id === 'dashboard' ? '/' : `/${item.id}`} className={screen === item.id ? 'active' : ''} onClick={(e) => { e.preventDefault(); navigate(item.id); }}>
            <span className="nav-icon">{item.icon}</span><span>{item.title}</span>{item.id === 'conflicts' && <em>2</em>}
          </a>)}
        </div>)}
      </nav>
      <div className="profile-card"><span className="avatar">Н</span><span><b>Никита</b><small>Владелец пространства</small></span><button aria-label="Настройки профиля">•••</button></div>
    </aside>

    <div className="main-column">
      <header className="topbar">
        <button className="menu-button" onClick={() => setSidebarOpen(!sidebarOpen)} aria-label="Открыть меню">☰</button>
        <button className="workspace-switcher"><span>Личное</span><small>⌄</small></button>
        <button className="search-button" onClick={() => setCommandOpen(true)}><span>⌕</span><span>Найти операцию, счёт, категорию…</span><kbd>Ctrl K</kbd></button>
        <div className="top-actions">
          <button className="period-button">Август 2026 <span>⌄</span></button>
          <button className="icon-button" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} aria-label="Сменить тему">{theme === 'dark' ? '☼' : '◐'}</button>
          <button className="icon-button notification" aria-label="Уведомления">♢<i /></button>
          <button className="primary-button compact" onClick={() => setAddOpen(true)}>＋ Операция</button>
        </div>
      </header>
      <main className="content">
        <div className="page-heading"><div><p className="eyebrow">Финпространство · Август 2026</p><h1>{screenTitles[screen][0]}</h1><p className="subtitle">{screenTitles[screen][1]}</p></div>
          {screen === 'dashboard' && <button className="secondary-button" onClick={() => flash('Данные обновлены')}>↻ Обновить</button>}
          {screen === 'budget' && <button className="secondary-button" onClick={() => flash('План прошлого месяца скопирован')}>Копировать июль</button>}
          {screen === 'transactions' && <button className="secondary-button" onClick={() => flash('CSV-экспорт подготовлен')}>Экспорт CSV</button>}
        </div>
        {screen === 'dashboard' && <Dashboard onNavigate={navigate} onAdd={() => setAddOpen(true)} />}
        {screen === 'budget' && <Budget />}
        {screen === 'transactions' && <Transactions selected={selected} setSelected={setSelected} onAdd={() => setAddOpen(true)} flash={flash} />}
        {!['dashboard', 'budget', 'transactions'].includes(screen) && <SecondaryScreen screen={screen} navigate={navigate} flash={flash} />}
      </main>
    </div>

    <nav className="mobile-nav" aria-label="Мобильная навигация">
      {[['dashboard','⌂','Сегодня'],['budget','◫','Бюджет'],['transactions','↕','Операции'],['accounts','▣','Счета']].map(([id, icon, label]) => <button key={id} className={screen === id ? 'active' : ''} onClick={() => navigate(id as Screen)}><span>{icon}</span><small>{label}</small></button>)}
      <button onClick={() => setSidebarOpen(true)}><span>•••</span><small>Ещё</small></button>
    </nav>
    {addOpen && <AddTransaction onClose={() => setAddOpen(false)} onSave={() => { setAddOpen(false); flash('Операция добавлена'); }} />}
    {commandOpen && <CommandPalette onClose={() => setCommandOpen(false)} onNavigate={(next) => { navigate(next); setCommandOpen(false); }} />}
    {sidebarOpen && <div className="mobile-scrim" onClick={() => setSidebarOpen(false)} />}
    {notice && <div className="toast" role="status">✓ {notice}</div>}
  </div>;
}

function Dashboard({ onNavigate, onAdd }: { onNavigate: (s: Screen) => void; onAdd: () => void }) {
  return <>
    <section className="metric-grid">
      <Metric label="Доступно до конца месяца" value="54 840 ₽" note="5 484 ₽ в день" tone="green" trend="+12% к июлю" />
      <Metric label="Потрачено в августе" value="87 160 ₽" note="из 142 000 ₽" tone="neutral" trend="61% бюджета" />
      <Metric label="Чистый денежный поток" value="+45 940 ₽" note="142 500 ₽ − 96 560 ₽" tone="green" trend="выше плана" />
      <Metric label="Резерв" value="318 400 ₽" note="3,4 месяца расходов" tone="neutral" trend="цель: 6 месяцев" />
    </section>
    <section className="dashboard-grid">
      <div className="card cashflow-card"><CardHeader eyebrow="Денежный поток" title="Доходы и расходы" action="За 6 месяцев" />
        <div className="chart-summary"><div><span className="dot income" />Доходы <b>814 200 ₽</b></div><div><span className="dot expense" />Расходы <b>576 340 ₽</b></div></div>
        <div className="bar-chart" aria-label="График доходов и расходов">{[['Мар',66,42],['Апр',72,55],['Май',61,48],['Июн',84,52],['Июл',70,59],['Авг',94,62]].map(([m,a,b]) => <div className="bar-group" key={String(m)}><div><i className="bar income-bar" style={{height:`${a}%`}}/><i className="bar expense-bar" style={{height:`${b}%`}}/></div><span>{m}</span></div>)}</div>
      </div>
      <div className="card budget-health"><CardHeader eyebrow="Бюджет" title="Темп расходов" action="Август" />
        <div className="donut"><div><b>61%</b><span>использовано</span></div></div><div className="budget-health-copy"><b>Идёте по плану</b><p>До конца месяца 9 дней. Запас относительно темпа — 7 680 ₽.</p><button className="text-button" onClick={() => onNavigate('budget')}>Открыть бюджет →</button></div>
      </div>
      <div className="card accounts-card"><CardHeader eyebrow="Счета" title="Текущие остатки" action="486 720 ₽" />
        <AccountRow icon="С" color="#21d79b" title="Сбер · Основной" meta="•• 1842" amount="146 280,40 ₽" /><AccountRow icon="Т" color="#f7d633" title="Т-Банк · Ежедневный" meta="•• 9031" amount="38 624,18 ₽" /><AccountRow icon="Н" color="#8b5cf6" title="Накопления" meta="Резерв" amount="301 815,42 ₽" />
        <button className="wide-button" onClick={() => onNavigate('accounts')}>Все счета</button>
      </div>
      <div className="card recent-card"><CardHeader eyebrow="Последнее" title="Недавние операции" action="Сегодня" />
        {transactionRows.slice(0,4).map((r) => <div className="recent-row" key={r[1]}><span className={`tx-icon ${r[5]}`}>{r[5] === 'income' ? '↓' : r[5] === 'transfer' ? '↔' : '↑'}</span><div><b>{r[1]}</b><small>{r[2]} · {r[0]}</small></div><strong className={r[5]}>{r[4]}</strong></div>)}
        <button className="wide-button" onClick={() => onNavigate('transactions')}>Все операции</button>
      </div>
    </section><button className="floating-add" onClick={onAdd} aria-label="Добавить операцию">＋</button>
  </>;
}

function Metric({label,value,note,tone,trend}:{label:string;value:string;note:string;tone:string;trend:string}) { return <article className="metric-card"><div><span>{label}</span><b className={tone}>{value}</b><small>{note}</small></div><em>{trend}</em></article>; }
function CardHeader({eyebrow,title,action}:{eyebrow:string;title:string;action:string}) { return <div className="card-header"><div><p>{eyebrow}</p><h2>{title}</h2></div><span>{action}</span></div>; }
function AccountRow({icon,color,title,meta,amount}:{icon:string;color:string;title:string;meta:string;amount:string}) { return <div className="account-row"><span style={{background:color}}>{icon}</span><div><b>{title}</b><small>{meta}</small></div><strong>{amount}</strong></div>; }

function Budget() {
  const [month, setMonth] = useState('Август 2026'); const totalPlanned = budgetRows.reduce((s,r)=>s+Number(r[1]),0); const totalSpent = budgetRows.reduce((s,r)=>s+Number(r[2]),0);
  return <><section className="budget-toolbar card"><button onClick={() => setMonth('Июль 2026')}>‹</button><div><small>Период бюджета</small><b>{month}</b></div><button onClick={() => setMonth('Сентябрь 2026')}>›</button><div className="budget-totals"><span><small>Запланировано</small><b>{totalPlanned.toLocaleString('ru-RU')} ₽</b></span><span><small>Потрачено</small><b>{totalSpent.toLocaleString('ru-RU')} ₽</b></span><span className="positive"><small>Осталось</small><b>{(totalPlanned-totalSpent).toLocaleString('ru-RU')} ₽</b></span></div></section>
    <section className="budget-layout"><div className="card budget-table-card"><div className="table-title"><div><h2>План на месяц</h2><p>Нажмите на сумму, чтобы изменить план</p></div><button className="ghost-button">＋ Категория</button></div>
      <div className="budget-table"><div className="budget-head"><span>Категория</span><span>План</span><span>Потрачено</span><span>Осталось</span></div>
        {budgetRows.map(([name, planned, spent, group, color]) => { const left = Number(planned)-Number(spent); const over = left < 0; return <div className="budget-row" key={String(name)}><span className="category-cell"><i style={{background:String(color)}}/><span><b>{name}</b><small>{group}</small></span></span><span className="editable" contentEditable suppressContentEditableWarning>{Number(planned).toLocaleString('ru-RU')} ₽</span><span>{Number(spent).toLocaleString('ru-RU')} ₽<i className="mini-progress"><u style={{width:`${Math.min(100,Number(spent)/Number(planned)*100)}%`}}/></i></span><span className={over?'negative':'positive'}>{left.toLocaleString('ru-RU')} ₽{over && <small>перерасход</small>}</span></div>; })}
      </div></div><aside className="budget-side"><div className="card assignment-card"><p className="eyebrow">Нужно распределить</p><h2>12 500 ₽</h2><p>Остаток дохода без назначения. Распределите его по категориям или целям.</p><button className="primary-button">Распределить</button></div><div className="card"><CardHeader eyebrow="Подсказка" title="Перерасход" action="1 категория"/><p className="muted-copy">«Кафе и рестораны» превышен на 1 320 ₽. Можно покрыть из «Развлечений».</p><button className="wide-button">Исправить бюджет</button></div></aside></section>
  </>;
}

function Transactions({selected,setSelected,onAdd,flash}:{selected:number[];setSelected:(v:number[])=>void;onAdd:()=>void;flash:(s:string)=>void}) {
  const toggle = (i:number) => setSelected(selected.includes(i)?selected.filter(x=>x!==i):[...selected,i]);
  return <section className="card transactions-card"><div className="transaction-toolbar"><div className="filter-search">⌕ <input aria-label="Поиск операций" placeholder="Получатель, сумма или заметка"/></div><button className="filter-chip active">Все</button><button className="filter-chip">Расходы</button><button className="filter-chip">Доходы</button><button className="filter-chip">Переводы</button><button className="filter-chip">＋ Фильтр</button></div>
    <div className="transaction-summary"><span>Найдено: <b>247 операций</b></span><span>Доходы <b className="positive">142 500 ₽</b></span><span>Расходы <b>96 560 ₽</b></span></div>
    {selected.length > 0 && <div className="bulk-bar"><b>Выбрано: {selected.length}</b><button onClick={()=>flash('Категория изменена')}>Изменить категорию</button><button onClick={()=>flash('Операции помечены проверенными')}>Проверено</button><button onClick={()=>setSelected([])}>Снять выбор</button></div>}
    <div className="tx-table"><div className="tx-head"><span></span><span>Дата</span><span>Получатель</span><span>Категория</span><span>Счёт</span><span>Сумма</span><span></span></div>{transactionRows.map((r,i)=><div className={`tx-row ${selected.includes(i)?'selected':''}`} key={`${r[0]}${r[1]}`}><span><input type="checkbox" checked={selected.includes(i)} onChange={()=>toggle(i)} aria-label={`Выбрать ${r[1]}`}/></span><span>{r[0]}</span><span><b>{r[1]}</b>{i===0&&<small>Обычная покупка</small>}</span><span><em>{r[2]}</em></span><span>{r[3]}</span><span className={r[5]}>{r[4]}</span><span><button aria-label="Меню операции">•••</button></span></div>)}</div>
    <div className="table-footer"><span>1–7 из 247</span><div><button disabled>←</button><button>→</button></div></div><button className="floating-add" onClick={onAdd} aria-label="Добавить операцию">＋</button>
  </section>;
}

function SecondaryScreen({screen,navigate,flash}:{screen:Screen;navigate:(s:Screen)=>void;flash:(s:string)=>void}) {
  const configs: Partial<Record<Screen,{kpis:string[][];title:string;rows:string[][];action:string}>> = {
    accounts: {kpis:[['Общий баланс','486 720 ₽'],['Активные счета','4'],['Нужно сверить','1 счёт']],title:'Все счета',rows:[['Сбер · Основной','146 280,40 ₽','сверен сегодня'],['Т-Банк · Ежедневный','38 624,18 ₽','сверен вчера'],['Накопления','301 815,42 ₽','сверен сегодня'],['Наличные','0 ₽','не сверялся']],action:'Сверить счёт'},
    reports: {kpis:[['Средние расходы','92 640 ₽'],['Норма сбережений','31%'],['Капитал','486 720 ₽']],title:'Расходы по категориям',rows:[['Продукты','22%','112 480 ₽'],['Дом','19%','97 200 ₽'],['Транспорт','12%','61 340 ₽'],['Развлечения','9%','46 170 ₽']],action:'Настроить отчёт'},
    payees: {kpis:[['Получателей','183'],['Без категории','6'],['Объединено','24']],title:'Частые получатели',rows:[['Перекрёсток','42 операции','Продукты'],['Яндекс Go','31 операция','Транспорт'],['МТС','8 операций','Связь'],['ООО «Альтаир»','6 операций','Доход']],action:'Объединить дубли'},
    rules: {kpis:[['Активных правил','12'],['Сработало в августе','84'],['Требуют проверки','2']],title:'Правила категоризации',rows:[['Содержит «Перекрёсток»','→ Продукты','84 совпадения'],['Содержит «Яндекс Go»','→ Транспорт','31 совпадение'],['Сумма = 750 ₽ и МТС','→ Связь','8 совпадений']],action:'Создать правило'},
    recurring: {kpis:[['В этом месяце','48 420 ₽'],['Ближайший платёж','завтра'],['Автоплатежей','7']],title:'Ближайшие платежи',rows:[['Аренда','1 сентября','32 000 ₽'],['МТС','3 сентября','750 ₽'],['Яндекс Плюс','5 сентября','399 ₽'],['Спортзал','7 сентября','2 500 ₽']],action:'Добавить регулярный'},
    goals: {kpis:[['Накоплено','301 815 ₽'],['Активных целей','3'],['В этом месяце','25 000 ₽']],title:'Финансовые цели',rows:[['Резерв','78%','301 815 из 390 000 ₽'],['Отпуск','42%','84 000 из 200 000 ₽'],['Новый ноутбук','18%','27 000 из 150 000 ₽']],action:'Новая цель'},
    import: {kpis:[['Импортировано','1 284'],['Пропущено дублей','36'],['Последний импорт','сегодня']],title:'История импорта',rows:[['Сбер · август.csv','сегодня','128 операций'],['Т-Банк · июль.xlsx','2 августа','96 операций'],['Сбер · июль.csv','1 августа','142 операции']],action:'Загрузить выписку'},
    google: {kpis:[['Статус','активен'],['Outbox','0'],['Конфликты','2']],title:'Apps Script Bridge',rows:[['Таблица','Finspace — основная','подключена'],['Последний heartbeat','сегодня, 14:30','активен'],['Последний pull','сегодня, 14:25','без ошибок']],action:'Открыть таблицу'},
    telegram: {kpis:[['Статус бота','активен'],['Команд сегодня','7'],['Ошибок','0']],title:'Telegram-автоматизации',rows:[['Утренняя сводка','каждый день, 08:30','включена'],['Быстрый расход','по команде','включён'],['Контроль бюджета','при 80%','включён']],action:'Открыть настройки бота'},
    conflicts: {kpis:[['Открыто','2'],['Решено за месяц','14'],['Критичных','0']],title:'Очередь конфликтов',rows:[['Перекрёсток · 2 438,60 ₽','сайт новее','сегодня, 14:32'],['Транспорт · 684 ₽','таблица новее','вчера, 18:04']],action:'Начать разбор'},
    close: {kpis:[['Готовность','92%'],['Несверенных счетов','1'],['Конфликтов','2']],title:'Чек-лист августа',rows:[['Все операции категоризированы','готово','247 из 247'],['Счета сверены','внимание','3 из 4'],['Конфликты решены','внимание','0 из 2'],['Резервная копия','готово','сегодня, 03:00']],action:'Закрыть август'},
    settings: {kpis:[['Участников','2'],['Роль','владелец'],['Валюта','RUB']],title:'Пространство «Личное»',rows:[['Часовой пояс','Asia/Yekaterinburg','UTC+5'],['Основная валюта','Российский рубль','RUB'],['Резервное копирование','ежедневно','03:00'],['Аудит','включён','90 дней']],action:'Сохранить настройки'},
  };
  const c = configs[screen] ?? configs.accounts!;
  return <><section className="secondary-kpis">{c.kpis.map(([l,v])=><div className="card" key={l}><small>{l}</small><b>{v}</b></div>)}</section><section className="card secondary-card"><div className="table-title"><div><h2>{c.title}</h2><p>Данные обновлены несколько секунд назад</p></div><button className="primary-button" onClick={()=>flash(`${c.action}: действие выполнено`)}>{c.action}</button></div><div className="simple-table">{c.rows.map((row,i)=><div key={row[0]}><span className="row-number">{String(i+1).padStart(2,'0')}</span><b>{row[0]}</b><span>{row[1]}</span><em>{row[2]}</em><button>→</button></div>)}</div>{screen === 'conflicts' && <div className="api-note">Для массового разрешения конфликтов требуется API-поддержка пакетных операций.</div>}{screen === 'reports' && <div className="report-bars">{c.rows.map((r,i)=><i key={r[0]} style={{height:`${[88,72,55,40][i]}%`}}><span>{r[0]}</span></i>)}</div>}</section>{screen === 'accounts' && <button className="text-button" onClick={()=>navigate('transactions')}>Посмотреть все операции →</button>}</>;
}

function AddTransaction({onClose,onSave}:{onClose:()=>void;onSave:()=>void}) {
  const [type,setType]=useState('Расход');
  return <div className="modal-layer" role="dialog" aria-modal="true" aria-label="Новая операция" onMouseDown={(e)=>{if(e.target===e.currentTarget)onClose();}}><section className="transaction-drawer"><header><div><p className="eyebrow">Новая запись</p><h2>Добавить операцию</h2></div><button onClick={onClose} aria-label="Закрыть">×</button></header><div className="segmented">{['Расход','Доход','Перевод'].map(x=><button key={x} className={type===x?'active':''} onClick={()=>setType(x)}>{x}</button>)}</div><label className="amount-field"><span>Сумма</span><div><input autoFocus defaultValue="2 450" inputMode="decimal"/><b>₽</b></div></label><div className="form-grid"><label><span>Счёт</span><select defaultValue="Сбер"><option>Сбер · Основной</option><option>Т-Банк · Ежедневный</option><option>Наличные</option></select></label><label><span>Дата</span><input type="date" defaultValue="2026-08-22"/></label></div><label><span>Получатель</span><input placeholder="Например, Перекрёсток" defaultValue="Перекрёсток"/></label><label><span>Категория</span><select defaultValue="Продукты"><option>Продукты</option><option>Транспорт</option><option>Дом</option><option>Кафе и рестораны</option></select></label><label><span>Заметка</span><textarea placeholder="Необязательно" rows={3}/></label><button className="split-link">＋ Разделить на категории</button><footer><button className="secondary-button" onClick={onClose}>Отмена</button><button className="primary-button" onClick={onSave}>Сохранить операцию</button></footer></section></div>;
}

function CommandPalette({onClose,onNavigate}:{onClose:()=>void;onNavigate:(s:Screen)=>void}) {
  const [query,setQuery]=useState(''); const items = useMemo(()=>navGroups.flatMap(g=>g.items).filter(i=>i.title.toLowerCase().includes(query.toLowerCase())),[query]);
  return <div className="command-layer" onMouseDown={(e)=>{if(e.target===e.currentTarget)onClose();}}><section className="command-box"><div className="command-input">⌕<input autoFocus placeholder="Найти раздел или выполнить действие…" value={query} onChange={e=>setQuery(e.target.value)}/><kbd>Esc</kbd></div><p>Быстрый переход</p>{items.slice(0,7).map(item=><button key={item.id} onClick={()=>onNavigate(item.id)}><span className="nav-icon">{item.icon}</span><b>{item.title}</b><small>Открыть</small><em>↵</em></button>)}</section></div>;
}
