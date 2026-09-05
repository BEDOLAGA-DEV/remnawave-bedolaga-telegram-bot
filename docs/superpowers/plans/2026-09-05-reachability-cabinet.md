# Доступность из РФ (bschekbot) — план реализации, кабинет

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Раздел админки «Доступность из РФ» в кабинете (`~/WebstormProjects/bedolaga-cabinet`): статус и баланс, выбор симок и целей, запуск с ценой, прогресс задачи, результаты трёх видов, сводка хост × симка, история, ярлыки с карточки ноды и подписки пользователя.

**Architecture:** Один маршрут `/admin/reachability` с вкладками через `?tab=`; тонкая страница, вся логика в `src/components/admin/reachability/`. Общий `LaunchPanel` для всех видов проверки, общий `UnitPicker`, хук `useReachabilityJob` опрашивает нашу задачу раз в 3 с. Данные — TanStack Query поверх `src/api/reachability.ts`, контракт которого задан планом бота (часть 2, Task 15).

**Tech Stack:** React 19, TypeScript, Vite, react-router 8, TanStack Query 5, Zustand (права), react-i18next, Tailwind (токены палитры), Radix-примитивы (`Sheet`, `Button`, `Switch`), Phosphor через баррель `@/components/icons`, vitest + jsdom + @testing-library/react.

**Spec:** `~/PycharmProjects/remnawave-bedolaga-telegram-bot/docs/superpowers/specs/2026-09-05-reachability-bschek-design.md` (разделы 9, 10, 11). Контракт API — план бота часть 2, Task 15 (схемы).

## Global Constraints

- Дизайн-канон кабинета (`CLAUDE.md`, раздел Design Canon): заголовок админки `text-xl font-bold text-dark-100`; цвета только токенами `dark-*`, `accent-*`, `success-*`, `warning-*`, `error-*`, текст на заливках `text-on-*`; радиусы `bento-card`/`rounded-3xl` → `rounded-2xl` → `rounded-xl`; кнопки `.btn-primary`/`.btn-secondary`/`.btn-ghost`/`.btn-danger` или примитив `Button`; иконки только из `@/components/icons`; скелетоны только `Skeleton`/`SkeletonGroup`.
- Каждый новый ключ локали — сразу во все четыре файла `src/locales/{ru,en,zh,fa}.json` (тест `src/locales/locales.test.ts` требует паритет en/ru; `admin.settings.categories.*` — только ru).
- Пункт меню обязателен для маршрута (`src/pages/adminNavCoverage.test.ts`).
- Никаких скачиваний файлов (в Mini App выбрасывает из приложения) — только копирование в буфер.
- `localStorage` — только под `try/catch`, и только для удобств (последний выбор симок).
- Файлы ≤ 300 строк; компоненты по одной ответственности; никаких прямых импортов `react-icons/*`.
- Перед каждым коммитом: `npm run type-check && npm run lint && npm run format && npm run test`. Коммиты `<type>(reachability): …` без строк атрибуции.
- Тесты хуков/компонентов — с прагмой `// @vitest-environment jsdom` в первой строке файла (см. `src/i18nColdCache.test.tsx`).
- Рабочая директория всех команд этого плана — `~/WebstormProjects/bedolaga-cabinet`.

## Карта файлов

| Файл | Ответственность |
|---|---|
| `src/api/reachability.ts` | типы контракта и вызовы API |
| `src/components/icons/extended-icons.tsx` | `CellSignalIcon` |
| `src/pages/AdminPanel.tsx`, `src/App.tsx` | пункт меню, иконка, маршрут |
| `src/components/admin/constants.ts` | подраздел настроек `sys_reachability` → `['BSCHEK']` |
| `src/pages/AdminReachability.tsx` | тонкая страница: шапка, `StatusBar`, вкладки |
| `src/components/admin/reachability/money.ts`, `verdict.ts`, `deepLink.ts`, `unitSelection.ts` | чистые модули (+ тесты) |
| `.../useReachabilityStatus.ts`, `useReachabilityJob.ts` | хуки данных (+ тест хука задачи) |
| `.../StatusBar.tsx`, `UnitPicker.tsx`, `LaunchPanel.tsx`, `JobProgress.tsx` | общие блоки |
| `.../HostsTargetList.tsx`, `NodesTargetList.tsx`, `SubscriptionConfigs.tsx`, `CustomTargetInput.tsx`, `CidrInput.tsx` | выбор целей |
| `.../ProbeTab.tsx`, `VlessTab.tsx`, `ScanTab.tsx` | сборка вкладок запуска |
| `.../ProbeResult.tsx`, `VlessResult.tsx`, `ScanResult.tsx`, `JobResult.tsx` | результаты |
| `.../HostsSummaryMatrix.tsx`, `SummaryTab.tsx`, `JobsHistory.tsx` | сводка и история |
| `src/pages/AdminRemnawave.tsx`, `src/components/admin/userDetail/SubscriptionTab.tsx`, `src/pages/AdminUserDetail.tsx` | ярлыки |
| `src/locales/*.json` | `admin.nav.reachability`, `admin.reachability.*`, `admin.settings.tree.sys_reachability` (ключ — рядом с `sys_remnawave`), `admin.settings.categories.BSCHEK` (ru) |

---

### Task C1: Модуль API и деньги

**Files:**
- Create: `src/api/reachability.ts`
- Create: `src/components/admin/reachability/money.ts`, `money.test.ts`

**Interfaces:**
- Produces типы `ReachabilityStatus`, `Unit`, `HostTarget`, `NodeTarget`, `SubscriptionConfigs`, `TargetIn`, `JobCreateRequest`, `PreviewResponse`, `Job`, `Leg`, `JobList`, `Summary`, `PrefUpdate`; объект `reachabilityApi` с методами `getStatus`, `getUnits(params)`, `getHosts(includeDisabled)`, `getNodes()`, `getSubscriptionConfigs({shortUuid, userId})`, `updatePref(body)`, `previewJob(body)`, `createJob(body)`, `listJobs(params)`, `getJob(id)`, `cancelJob(id)`, `retrieveJob(id)`, `getSummary(dpi)`.
- `formatKopeks(kopeks: number | null | undefined): string` → `'2,79 ₽'`, `'—'` для null.

- [ ] **Step 1: Падающий тест** `money.test.ts`

```ts
import { describe, expect, it } from 'vitest';
import { formatKopeks } from './money';

describe('formatKopeks', () => {
  it.each([
    [279, '2,79 ₽'],
    [100018, '1000,18 ₽'],
    [5, '0,05 ₽'],
    [0, '0,00 ₽'],
  ])('%s → %s', (kopeks, expected) => {
    expect(formatKopeks(kopeks)).toBe(expected);
  });

  it('пусто для null/undefined', () => {
    expect(formatKopeks(null)).toBe('—');
    expect(formatKopeks(undefined)).toBe('—');
  });
});
```

- [ ] **Step 2: Убедиться, что падает** — `npm run test -- money`.

- [ ] **Step 3: Реализация**

`money.ts`:

```ts
/** 1 кредит bschekbot = 1 копейка. Показываем рубли с копейками через запятую. */
export function formatKopeks(kopeks: number | null | undefined): string {
  if (kopeks === null || kopeks === undefined) return '—';
  const rub = Math.trunc(kopeks / 100);
  const kop = Math.abs(kopeks % 100);
  return `${rub},${String(kop).padStart(2, '0')} ₽`;
}
```

`src/api/reachability.ts`:

```ts
import { apiClient } from './client';

// === Типы контракта (app/cabinet/schemas/reachability.py) ===

export type JobKind = 'probe' | 'vless' | 'scan';
export type Dpi = 'on' | 'off' | 'any';
export type Purpose = 'bs' | 'regular' | 'unknown';
export type Verdict = 'reachable' | 'blocked' | 'down' | 'unknown' | 'cancelled';
export type JobStatus = 'pending' | 'running' | 'done' | 'failed' | 'cancelled';

export interface Unit {
  op_key: string;
  operator: string;
  name: string;
  region: string;
  region_code: string;
  dpi: string;
  channel_state: string;
  probeable: boolean;
}

export interface ActiveJob {
  id: number;
  kind: JobKind;
  phase: string | null;
  started_by_user_id: number | null;
  started_at: string | null;
}

export interface ReachabilityStatus {
  enabled: boolean;
  configured: boolean;
  healthy: boolean;
  health_message: string | null;
  balance_kopeks: number | null;
  bonus_kopeks: number | null;
  tier: string | null;
  tier_expires_at: string | null;
  min_interval_sec: number | null;
  active_jobs: ActiveJob[];
  reference: { short_uuid: string | null; configs: number; error: string | null } | null;
  cost_limit_kopeks: number;
}

export interface HostTarget {
  uuid: string;
  remark: string;
  address: string;
  port: number | null;
  sni: string | null;
  is_disabled: boolean;
  tag: string | null;
  purpose: Purpose;
  purpose_guessed: boolean;
  excluded: boolean;
  node_uuids: string[];
  target_key: string;
}

export interface NodeTarget {
  uuid: string;
  name: string;
  address: string;
  is_connected: boolean;
  is_disabled: boolean;
  host_uuids: string[];
  target_key: string;
}

export interface SubscriptionConfig {
  index: number;
  protocol: string | null;
  label: string;
  address: string;
  port: number | null;
  sni: string | null;
  target_key: string;
  purpose: Purpose;
}

export interface SubscriptionConfigs {
  short_uuid: string;
  configs: SubscriptionConfig[];
  rejected: { reason: string; preview: string }[];
}

export interface TargetIn {
  kind: 'host' | 'node' | 'subscription_config' | 'custom' | 'cidr';
  ref?: string;
  value?: string;
  short_uuid?: string;
  index?: number;
}

export interface JobCreateRequest {
  kind: JobKind;
  targets: TargetIn[];
  units: string[];
  dpi: Dpi;
  probes: { icmp: boolean; tcp: boolean; sni: boolean };
  core: '' | 'stable' | 'prerelease';
}

export interface Skipped {
  dpi_off: Array<Partial<Unit> & { op_key?: string }>;
  unavailable: Array<Partial<Unit> & { op_key?: string }>;
  unknown: string[];
  blocked_targets: Array<{ target?: string; reason?: string }>;
}

export interface TargetOut {
  kind: string;
  label: string;
  address: string;
  port: number | null;
  target_key: string;
  sni: string | null;
  ref: Record<string, unknown>;
  purpose: Purpose;
}

export interface PreviewResponse {
  kind: JobKind;
  targets: TargetOut[];
  units_resolved: string[];
  skipped: Skipped;
  cost_kopeks: number | null;
  estimate_is_exact: boolean;
  warnings: string[];
  balance_kopeks: number | null;
}

export interface Leg {
  id: number;
  target_key: string;
  target_kind: string | null;
  target_ref: string | null;
  op_key: string;
  operator: string | null;
  region: string | null;
  dpi: string | null;
  verdict: Verdict;
  matches_expectation: boolean | null;
  raw: Record<string, unknown> | null;
  checked_at: string;
}

export interface Job {
  id: number;
  kind: JobKind;
  status: JobStatus;
  phase: string | null;
  trigger: string;
  started_by_user_id: number | null;
  external_id: number | null;
  targets: TargetOut[];
  units_requested: string[] | null;
  units_resolved: string[] | null;
  units_effective: string[] | null;
  skipped: Skipped | null;
  dpi: Dpi;
  estimated_kopeks: number | null;
  estimate_is_exact: boolean;
  cost_kopeks: number | null;
  refunded_kopeks: number | null;
  result: Record<string, unknown> | null;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean | null;
  attempts: number;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  legs: Leg[];
}

export interface JobList {
  items: Job[];
  total: number;
  offset: number;
  limit: number;
}

export interface SummaryCell {
  verdict: Verdict;
  matches_expectation: boolean | null;
  checked_at: string;
  job_id: number;
}

export interface Summary {
  units: Unit[];
  rows: Array<{
    target_key: string;
    kind: string | null;
    ref: string | null;
    purpose: Purpose;
    cells: Record<string, SummaryCell>;
  }>;
}

export interface PrefUpdate {
  target_kind: 'host' | 'node';
  target_ref: string;
  purpose?: Purpose;
  excluded?: boolean;
  note?: string;
}

const BASE = '/cabinet/admin/reachability';

export const reachabilityApi = {
  getStatus: async (): Promise<ReachabilityStatus> => (await apiClient.get(`${BASE}/status`)).data,

  getUnits: async (params: { dpi?: Dpi; operator?: string; region?: string } = {}): Promise<Unit[]> =>
    (await apiClient.get(`${BASE}/units`, { params })).data.units,

  getHosts: async (includeDisabled = false): Promise<HostTarget[]> =>
    (await apiClient.get(`${BASE}/targets/hosts`, { params: { include_disabled: includeDisabled } })).data
      .items,

  getNodes: async (): Promise<NodeTarget[]> => (await apiClient.get(`${BASE}/targets/nodes`)).data.items,

  getSubscriptionConfigs: async (params: {
    shortUuid?: string;
    userId?: number;
  }): Promise<SubscriptionConfigs> =>
    (
      await apiClient.get(`${BASE}/targets/subscription`, {
        params: { short_uuid: params.shortUuid, user_id: params.userId },
      })
    ).data,

  updatePref: async (body: PrefUpdate) => (await apiClient.put(`${BASE}/targets/prefs`, body)).data,

  previewJob: async (body: JobCreateRequest): Promise<PreviewResponse> =>
    (await apiClient.post(`${BASE}/jobs/preview`, body)).data,

  createJob: async (body: JobCreateRequest): Promise<Job> => (await apiClient.post(`${BASE}/jobs`, body)).data,

  listJobs: async (params: {
    kind?: JobKind;
    status?: JobStatus;
    target_key?: string;
    user_id?: number;
    offset?: number;
    limit?: number;
  } = {}): Promise<JobList> => (await apiClient.get(`${BASE}/jobs`, { params })).data,

  getJob: async (id: number): Promise<Job> => (await apiClient.get(`${BASE}/jobs/${id}`)).data,

  cancelJob: async (id: number): Promise<Job> => (await apiClient.post(`${BASE}/jobs/${id}/cancel`)).data,

  retrieveJob: async (id: number): Promise<Job> => (await apiClient.post(`${BASE}/jobs/${id}/retrieve`)).data,

  getSummary: async (dpi: Dpi = 'on'): Promise<Summary> =>
    (await apiClient.get(`${BASE}/summary/hosts`, { params: { dpi } })).data,
};
```

Проверить, как `apiClient` экспортируется из `./client` (в `adminRemnawave.ts` — именованный импорт `{ apiClient }`, в `banSystem.ts` — default): взять тот, что есть.

- [ ] **Step 4: Прогнать** — `npm run test -- money && npm run type-check`.

- [ ] **Step 5: Коммит**

```bash
npm run lint && npm run format
git add src/api/reachability.ts src/components/admin/reachability/money.ts src/components/admin/reachability/money.test.ts
git commit -m "feat(reachability): модуль API раздела и форматирование копеек"
```

---

### Task C2: Маршрут, меню, иконка, страница-каркас, локали

**Files:**
- Modify: `src/components/icons/extended-icons.tsx` (импорт `PiCellSignalFull`, экспорт `CellSignalIcon`)
- Modify: `src/pages/AdminPanel.tsx` (импорт иконки; в `icons`: `signal: <CellSignalIcon />`; в группе `system` после пункта `remnawave`: `{ name: 'admin.nav.reachability', icon: 'signal', to: '/admin/reachability', permission: 'reachability:read' }`)
- Modify: `src/App.tsx` (lazy import рядом с `AdminBanSystem`; `<Route path="/admin/reachability" element={<PermissionRoute permission="reachability:read"><LazyPage><AdminReachability /></LazyPage></PermissionRoute>} />`)
- Create: `src/pages/AdminReachability.tsx`, `src/components/admin/reachability/useReachabilityStatus.ts`, `StatusBar.tsx`
- Modify: `src/locales/{ru,en,zh,fa}.json`

- [ ] **Step 1: Убедиться, что тест меню сейчас упадёт, если добавить маршрут без пункта** — не требуется; пункт и маршрут добавляются вместе. Прогнать `npm run test -- adminNavCoverage` до и после.

- [ ] **Step 2: Иконка**

В `extended-icons.tsx` добавить `PiCellSignalFull` в импорт из `react-icons/pi` и:

```tsx
export const CellSignalIcon = ({ className }: IconProps) => (
  <PiCellSignalFull className={cn('h-5 w-5', className)} />
);
```

- [ ] **Step 3: Хук статуса** `useReachabilityStatus.ts`

```ts
import { useQuery } from '@tanstack/react-query';
import { reachabilityApi, type ReachabilityStatus } from '@/api/reachability';

export const REACHABILITY_STATUS_KEY = ['admin-reachability-status'] as const;

/** Статус интеграции: баланс, тариф, занятость. Обновляется редко и после завершения задач. */
export function useReachabilityStatus(enabled = true) {
  return useQuery<ReachabilityStatus>({
    queryKey: REACHABILITY_STATUS_KEY,
    queryFn: reachabilityApi.getStatus,
    enabled,
    staleTime: 60_000,
    retry: false,
  });
}

/** Для ярлыков на чужих страницах: показывать кнопку только при включённой интеграции. */
export function useReachabilityAvailable(): boolean {
  const { data } = useReachabilityStatus();
  return Boolean(data?.enabled && data?.configured);
}
```

- [ ] **Step 4: StatusBar** `StatusBar.tsx`

```tsx
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import type { ReachabilityStatus } from '@/api/reachability';
import { Skeleton } from '@/components/ui/skeleton';
import { formatKopeks } from './money';

interface StatusBarProps {
  status: ReachabilityStatus | undefined;
  isLoading: boolean;
}

export function StatusBar({ status, isLoading }: StatusBarProps) {
  const { t } = useTranslation();

  if (isLoading) {
    return <Skeleton className="h-16 w-full rounded-2xl" />;
  }
  if (!status) return null;

  if (!status.enabled || !status.configured) {
    return (
      <div className="rounded-2xl border border-warning-500/30 bg-warning-500/10 p-4 text-sm text-dark-100">
        <p className="font-medium">
          {t(status.enabled ? 'admin.reachability.status.notConfigured' : 'admin.reachability.status.disabled')}
        </p>
        <Link to="/admin/settings" className="mt-2 inline-block text-accent-400 hover:underline">
          {t('admin.reachability.status.openSettings')}
        </Link>
      </div>
    );
  }

  return (
    <div className="grid gap-3 rounded-2xl border border-dark-700/60 bg-dark-800/60 p-4 sm:grid-cols-4">
      <Stat label={t('admin.reachability.status.balance')} value={formatKopeks(status.balance_kopeks)} />
      <Stat
        label={t('admin.reachability.status.tier')}
        value={status.tier ? `${status.tier} · ${formatDate(status.tier_expires_at)}` : '—'}
      />
      <Stat
        label={t('admin.reachability.status.reference')}
        value={status.reference?.error ?? t('admin.reachability.status.referenceConfigs', { count: status.reference?.configs ?? 0 })}
        tone={status.reference?.error ? 'warning' : 'default'}
      />
      <Stat
        label={t('admin.reachability.status.activity')}
        value={
          status.active_jobs.length === 0
            ? t('admin.reachability.status.idle')
            : status.active_jobs.map((job) => t(`admin.reachability.kinds.${job.kind}`)).join(', ')
        }
        tone={status.active_jobs.length ? 'accent' : 'default'}
      />
      {!status.healthy && (
        <p className="text-sm text-error-400 sm:col-span-4">{status.health_message}</p>
      )}
    </div>
  );
}

function Stat({ label, value, tone = 'default' }: { label: string; value: string; tone?: 'default' | 'warning' | 'accent' }) {
  const toneClass = tone === 'warning' ? 'text-warning-400' : tone === 'accent' ? 'text-accent-400' : 'text-dark-50';
  return (
    <div>
      <p className="text-xs uppercase tracking-wide text-dark-400">{label}</p>
      <p className={`mt-1 truncate text-sm font-semibold ${toneClass}`}>{value}</p>
    </div>
  );
}

function formatDate(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString();
}
```

- [ ] **Step 5: Страница-каркас** `src/pages/AdminReachability.tsx`

```tsx
import { useMemo } from 'react';
import { useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { AdminBackButton } from '../components/admin/AdminBackButton';
import { StatusBar } from '../components/admin/reachability/StatusBar';
import { useReachabilityStatus } from '../components/admin/reachability/useReachabilityStatus';
import { parseReachabilityDeepLink, type TabKey, TAB_KEYS } from '../components/admin/reachability/deepLink';
import { SummaryTab } from '../components/admin/reachability/SummaryTab';
import { ProbeTab } from '../components/admin/reachability/ProbeTab';
import { VlessTab } from '../components/admin/reachability/VlessTab';
import { ScanTab } from '../components/admin/reachability/ScanTab';
import { JobsHistory } from '../components/admin/reachability/JobsHistory';
import { cn } from '@/lib/utils';

export default function AdminReachability() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const link = useMemo(() => parseReachabilityDeepLink(searchParams), [searchParams]);
  const { data: status, isLoading } = useReachabilityStatus();

  const setTab = (tab: TabKey) => {
    const next = new URLSearchParams(searchParams);
    next.set('tab', tab);
    setSearchParams(next, { replace: true });
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <AdminBackButton />
        <h1 className="text-xl font-bold text-dark-100">{t('admin.reachability.title')}</h1>
      </div>

      <StatusBar status={status} isLoading={isLoading} />

      <div className="flex gap-1 overflow-x-auto rounded-2xl bg-dark-800/60 p-1">
        {TAB_KEYS.map((tab) => (
          <button
            key={tab}
            type="button"
            onClick={() => setTab(tab)}
            className={cn(
              'whitespace-nowrap rounded-xl px-4 py-2 text-sm font-medium transition-colors',
              link.tab === tab ? 'bg-accent-500 text-on-accent' : 'text-dark-300 hover:bg-dark-700 hover:text-dark-100',
            )}
          >
            {t(`admin.reachability.tabs.${tab}`)}
          </button>
        ))}
      </div>

      {link.tab === 'summary' && <SummaryTab status={status} />}
      {link.tab === 'probe' && <ProbeTab status={status} preselected={link.targets} />}
      {link.tab === 'vless' && <VlessTab status={status} userId={link.userId} shortUuid={link.shortUuid} />}
      {link.tab === 'scan' && <ScanTab status={status} />}
      {link.tab === 'history' && <JobsHistory />}
    </div>
  );
}
```

Пока вкладок нет (Tasks C3–C7), временно экспортировать из соответствующих файлов заглушки `export function ProbeTab() { return null; }` не нужно: выполнять C2 **после** C3 либо создать эти компоненты в C2 минимальными (`return <p className="text-dark-400">{t('admin.reachability.comingSoon')}</p>`) и заменить в своих задачах. Рекомендуется второй путь, чтобы маршрут и меню жили с первого коммита; ключ `comingSoon` удалить в C7.

- [ ] **Step 6: Локали** — в `ru.json`:

```json
"nav": { "reachability": "Доступность из РФ" },
"reachability": {
  "title": "Доступность из РФ",
  "comingSoon": "Раздел в разработке",
  "tabs": { "summary": "Сводка", "probe": "Проверка", "vless": "VLESS-тест", "scan": "Скан /24", "history": "История" },
  "kinds": { "probe": "проверка", "vless": "VLESS-тест", "scan": "скан /24" },
  "status": {
    "disabled": "Интеграция bschekbot выключена",
    "notConfigured": "Не задан ключ API bschekbot",
    "openSettings": "Открыть настройки",
    "balance": "Баланс",
    "tier": "Тариф",
    "reference": "Эталонная подписка",
    "referenceConfigs_one": "{{count}} конфиг",
    "referenceConfigs_few": "{{count}} конфига",
    "referenceConfigs_many": "{{count}} конфигов",
    "activity": "Сейчас",
    "idle": "свободно"
  },
  "units": {
    "title": "Симки операторов",
    "dpiOn": "с Белым списком",
    "dpiOff": "без Белого списка",
    "dpiAny": "все",
    "allRegions": "все округа",
    "selectAll": "выбрать все",
    "selectNone": "снять",
    "selected_one": "выбрана {{count}} симка",
    "selected_few": "выбрано {{count}} симки",
    "selected_many": "выбрано {{count}} симок",
    "notProbeable": "сейчас недоступна",
    "empty": "Список симок пуст — сервис недоступен"
  },
  "launch": {
    "price": "Цена",
    "estimate": "оценка, точная цена после запуска",
    "balance": "Остаток",
    "run": "Запустить за {{price}}",
    "running": "Запускаем…",
    "noTargets": "Выберите хотя бы одну цель",
    "noUnits": "Под фильтр Белого списка не попала ни одна симка",
    "overLimit": "Дороже потолка задачи ({{limit}})",
    "overBalance": "На балансе не хватает средств",
    "busy": "Уже идёт {{kind}} #{{id}} — дождитесь завершения",
    "skippedDpiOff": "Не попали под фильтр Белого списка: {{units}}",
    "skippedUnavailable": "Сейчас недоступны: {{units}}",
    "previewFailed": "Не удалось посчитать цену"
  },
  "progress": {
    "submitting": "Отправляем запрос…",
    "waiting": "Ждём ответа операторов ({{elapsed}})",
    "retrieving": "Проверка идёт дольше обычного, забираем результат ({{elapsed}})",
    "polling": "Проверка идёт ({{elapsed}})",
    "cancelling": "Отменяем…",
    "hintProbe": "Обычно 5–60 секунд, по всему флоту — до 10 минут",
    "hintVless": "До 3 минут на сервер",
    "hintScan": "15–40 секунд на симку",
    "stalled": "Опрос остановлен, результат появится в истории",
    "cancel": "Отменить",
    "retrieve": "Забрать результат",
    "failed": "Проверка не удалась",
    "retry": "Повторить",
    "cancelled": "Отменено"
  },
  "verdict": {
    "reachable": "доступен",
    "blocked": "режется",
    "down": "недоступен",
    "unknown": "неизвестно",
    "cancelled": "отменено",
    "asExpected": "как ожидалось",
    "unexpected": "не так, как ожидалось"
  },
  "purpose": { "bs": "под Белый список", "regular": "обычный", "unknown": "не определено", "guessed": "догадка" },
  "targets": {
    "hosts": "Хосты панели",
    "nodes": "Ноды",
    "custom": "Произвольная цель",
    "customPlaceholder": "IP, домен или адрес:порт",
    "customAdd": "Добавить",
    "includeDisabled": "показывать отключённые",
    "search": "Поиск по хостам",
    "nodePing": "только ping сервера",
    "nodeHosts_one": "{{count}} хост",
    "nodeHosts_few": "{{count}} хоста",
    "nodeHosts_many": "{{count}} хостов",
    "excluded": "исключён из сводки",
    "empty": "Панель не вернула хостов"
  },
  "subscription": {
    "title": "Конфиги подписки",
    "reference": "эталонная подписка",
    "pickUser": "Подставить подписку пользователя",
    "userSearchPlaceholder": "Имя, id или username",
    "configs_one": "{{count}} конфиг",
    "configs_few": "{{count}} конфига",
    "configs_many": "{{count}} конфигов",
    "rejected": "Пропущено: {{count}}",
    "limit": "Не больше 20 конфигов за тест",
    "core": "Ядро xray",
    "coreAuto": "авто",
    "coreStable": "stable",
    "corePrerelease": "prerelease"
  },
  "scan": {
    "cidr": "Подсеть /24",
    "cidrPlaceholder": "192.0.2.0/24",
    "fromHost": "подсеть этого хоста",
    "invalid": "Нужна ровно одна подсеть /24",
    "alive_one": "{{count}} живой адрес",
    "alive_few": "{{count}} живых адреса",
    "alive_many": "{{count}} живых адресов",
    "copy": "Скопировать список",
    "copied": "Скопировано"
  },
  "probes": { "icmp": "ICMP", "tcp": "TCP", "sni": "SNI", "title": "Пробы" },
  "result": {
    "cost": "Списано",
    "refunded": "возврат {{amount}}",
    "http": "HTTP",
    "tunnel": "туннель",
    "targetsOk": "цели {{ok}}/{{total}}",
    "latency": "{{ms}} мс",
    "core": "ядро {{core}}",
    "raw": "Подробности",
    "diagnosis": "Диагноз"
  },
  "summary": {
    "empty": "Проверок ещё не было — запустите первую на вкладке «Проверка»",
    "age": "{{age}} назад",
    "openJob": "Открыть задачу",
    "dpiFilter": "Симки"
  },
  "history": {
    "empty": "История пуста",
    "filters": { "kind": "Вид", "status": "Статус", "all": "все" },
    "columns": { "id": "#", "kind": "Вид", "targets": "Цели", "units": "Симки", "cost": "Списано", "status": "Статус", "started": "Запущена" },
    "statuses": { "pending": "в очереди", "running": "идёт", "done": "готово", "failed": "ошибка", "cancelled": "отменена" },
    "details": "Задача #{{id}}",
    "loadMore": "Показать ещё"
  }
},
"settings": { "tree": { "sys_reachability": "Доступность из РФ" }, "categories": { "BSCHEK": "Доступность из РФ (bschekbot)" } }
```

Ключи вкладываются в существующие объекты `admin.nav`, `admin.settings.tree` (рядом с `sys_remnawave`), `admin.settings.categories`; `admin.reachability` — новый объект. В `en.json` — те же ключи по-английски (плюралы `_one`/`_other`), в `zh.json` и `fa.json` — те же ключи на китайском и фарси (без `settings.categories.BSCHEK`). Английский блок:

```json
"reachability": {
  "title": "Reachability from Russia",
  "comingSoon": "Section under construction",
  "tabs": { "summary": "Summary", "probe": "Probe", "vless": "VLESS test", "scan": "Scan /24", "history": "History" },
  "kinds": { "probe": "probe", "vless": "VLESS test", "scan": "scan /24" },
  "status": { "disabled": "bschekbot integration is disabled", "notConfigured": "bschekbot API key is not set", "openSettings": "Open settings", "balance": "Balance", "tier": "Plan", "reference": "Reference subscription", "referenceConfigs_one": "{{count}} config", "referenceConfigs_other": "{{count}} configs", "activity": "Now", "idle": "idle" },
  "units": { "title": "Operator SIMs", "dpiOn": "with whitelist", "dpiOff": "without whitelist", "dpiAny": "all", "allRegions": "all regions", "selectAll": "select all", "selectNone": "clear", "selected_one": "{{count}} SIM selected", "selected_other": "{{count}} SIMs selected", "notProbeable": "unavailable now", "empty": "No SIMs — service unavailable" },
  "launch": { "price": "Price", "estimate": "estimate, exact price after launch", "balance": "Balance left", "run": "Run for {{price}}", "running": "Starting…", "noTargets": "Pick at least one target", "noUnits": "No SIM matches the whitelist filter", "overLimit": "Above the job price cap ({{limit}})", "overBalance": "Not enough balance", "busy": "{{kind}} #{{id}} is already running — wait for it", "skippedDpiOff": "Filtered out by whitelist mode: {{units}}", "skippedUnavailable": "Unavailable now: {{units}}", "previewFailed": "Could not compute the price" },
  "progress": { "submitting": "Sending request…", "waiting": "Waiting for operators ({{elapsed}})", "retrieving": "Taking longer than usual, fetching the result ({{elapsed}})", "polling": "Check in progress ({{elapsed}})", "cancelling": "Cancelling…", "hintProbe": "Usually 5–60 s, whole fleet up to 10 min", "hintVless": "Up to 3 min per server", "hintScan": "15–40 s per SIM", "stalled": "Polling stopped, the result will appear in History", "cancel": "Cancel", "retrieve": "Fetch result", "failed": "Check failed", "retry": "Retry", "cancelled": "Cancelled" },
  "verdict": { "reachable": "reachable", "blocked": "blocked", "down": "down", "unknown": "unknown", "cancelled": "cancelled", "asExpected": "as expected", "unexpected": "not as expected" },
  "purpose": { "bs": "whitelist bypass", "regular": "regular", "unknown": "undefined", "guessed": "guess" },
  "targets": { "hosts": "Panel hosts", "nodes": "Nodes", "custom": "Custom target", "customPlaceholder": "IP, domain or address:port", "customAdd": "Add", "includeDisabled": "show disabled", "search": "Search hosts", "nodePing": "server ping only", "nodeHosts_one": "{{count}} host", "nodeHosts_other": "{{count}} hosts", "excluded": "excluded from summary", "empty": "The panel returned no hosts" },
  "subscription": { "title": "Subscription configs", "reference": "reference subscription", "pickUser": "Use a user's subscription", "userSearchPlaceholder": "Name, id or username", "configs_one": "{{count}} config", "configs_other": "{{count}} configs", "rejected": "Skipped: {{count}}", "limit": "At most 20 configs per test", "core": "xray core", "coreAuto": "auto", "coreStable": "stable", "corePrerelease": "prerelease" },
  "scan": { "cidr": "Subnet /24", "cidrPlaceholder": "192.0.2.0/24", "fromHost": "this host's subnet", "invalid": "Exactly one /24 subnet is required", "alive_one": "{{count}} live address", "alive_other": "{{count}} live addresses", "copy": "Copy list", "copied": "Copied" },
  "probes": { "icmp": "ICMP", "tcp": "TCP", "sni": "SNI", "title": "Probes" },
  "result": { "cost": "Charged", "refunded": "refund {{amount}}", "http": "HTTP", "tunnel": "tunnel", "targetsOk": "targets {{ok}}/{{total}}", "latency": "{{ms}} ms", "core": "core {{core}}", "raw": "Details", "diagnosis": "Diagnosis" },
  "summary": { "empty": "No checks yet — run the first one on the Probe tab", "age": "{{age}} ago", "openJob": "Open job", "dpiFilter": "SIMs" },
  "history": { "empty": "History is empty", "filters": { "kind": "Kind", "status": "Status", "all": "all" }, "columns": { "id": "#", "kind": "Kind", "targets": "Targets", "units": "SIMs", "cost": "Charged", "status": "Status", "started": "Started" }, "statuses": { "pending": "queued", "running": "running", "done": "done", "failed": "failed", "cancelled": "cancelled" }, "details": "Job #{{id}}", "loadMore": "Show more" }
}
```

Плюс `"nav": { "reachability": "Reachability from Russia" }` и `"settings": { "tree": { "sys_reachability": "Reachability from Russia" } }`.

- [ ] **Step 7: Дерево настроек** — в `src/components/admin/constants.ts`, группа `system`, после `{ id: 'sys_remnawave', categories: ['REMNAWAVE'] }`:

```ts
        { id: 'sys_reachability', categories: ['BSCHEK'] },
```

- [ ] **Step 8: Прогнать** — `npm run test -- adminNavCoverage locales && npm run type-check`. Тест меню зелёный (маршрут в меню), паритет en/ru зелёный.

- [ ] **Step 9: Коммит**

```bash
npm run lint && npm run format
git add src/components/icons/extended-icons.tsx src/pages/AdminPanel.tsx src/App.tsx src/pages/AdminReachability.tsx src/components/admin/reachability src/components/admin/constants.ts src/locales
git commit -m "feat(reachability): раздел «Доступность из РФ» — маршрут, меню, статус, локали"
```

---

### Task C3: Чистые модули: ярлыки, вердикты, выбор симок

**Files:**
- Create: `src/components/admin/reachability/deepLink.ts`, `deepLink.test.ts`, `verdict.ts`, `verdict.test.ts`, `unitSelection.ts`, `unitSelection.test.ts`

**Interfaces:**
- `deepLink.ts`: `TAB_KEYS = ['summary','probe','vless','scan','history'] as const`, `type TabKey`, `interface DeepLink { tab: TabKey; targets: Array<{ kind: 'host' | 'node'; ref: string }>; userId: number | null; shortUuid: string | null }`, `parseReachabilityDeepLink(params: URLSearchParams): DeepLink`, `buildReachabilityLink(input: Partial<DeepLink>): string`.
- `verdict.ts`: `type Tone = 'success' | 'error' | 'warning' | 'neutral'`, `verdictTone(verdict: Verdict, matches: boolean | null): Tone`, `toneClasses(tone: Tone): string` (только токены), `verdictLabelKey(verdict: Verdict): string`.
- `unitSelection.ts`: `type DpiFilter = 'on' | 'off' | 'any'`, `filterUnits(units: Unit[], filter: { dpi: DpiFilter; region: string | null }): Unit[]`, `regionsOf(units): Array<{ code: string; label: string }>`, `toggleKey(selected: string[], key: string): string[]`, `loadSelection(kind: JobKind): string[]`, `saveSelection(kind: JobKind, keys: string[]): void`, `defaultDpiFor(purposes: Purpose[]): DpiFilter` (`bs` → `on`, `regular` → `off`, смешанные/пусто → `on`).

- [ ] **Step 1: Падающие тесты**

`deepLink.test.ts`:

```ts
import { describe, expect, it } from 'vitest';
import { buildReachabilityLink, parseReachabilityDeepLink } from './deepLink';

describe('parseReachabilityDeepLink', () => {
  it('по умолчанию — сводка без целей', () => {
    expect(parseReachabilityDeepLink(new URLSearchParams(''))).toEqual({ tab: 'summary', targets: [], userId: null, shortUuid: null });
  });

  it('target=host:<uuid> открывает проверку с целью, параметр повторяемый', () => {
    const link = parseReachabilityDeepLink(new URLSearchParams('target=host:h-1&target=node:n-2'));
    expect(link.tab).toBe('probe');
    expect(link.targets).toEqual([{ kind: 'host', ref: 'h-1' }, { kind: 'node', ref: 'n-2' }]);
  });

  it('user= ведёт на VLESS-тест, мусор в user игнорируется', () => {
    expect(parseReachabilityDeepLink(new URLSearchParams('tab=vless&user=15'))).toMatchObject({ tab: 'vless', userId: 15 });
    expect(parseReachabilityDeepLink(new URLSearchParams('user=abc')).userId).toBeNull();
  });

  it('неизвестная вкладка и неизвестный вид цели отбрасываются', () => {
    const link = parseReachabilityDeepLink(new URLSearchParams('tab=teapot&target=cidr:1.2.3.0'));
    expect(link.tab).toBe('summary');
    expect(link.targets).toEqual([]);
  });

  it('buildReachabilityLink собирает обратную ссылку', () => {
    expect(buildReachabilityLink({ targets: [{ kind: 'node', ref: 'n-1' }] })).toBe('/admin/reachability?tab=probe&target=node%3An-1');
    expect(buildReachabilityLink({ tab: 'vless', userId: 7 })).toBe('/admin/reachability?tab=vless&user=7');
  });
});
```

`verdict.test.ts`:

```ts
import { describe, expect, it } from 'vitest';
import { toneClasses, verdictTone } from './verdict';

describe('verdictTone', () => {
  it('цвет — по соответствию ожиданию, а не по самому вердикту', () => {
    expect(verdictTone('reachable', true)).toBe('success');
    expect(verdictTone('blocked', false)).toBe('error');
    expect(verdictTone('blocked', null)).toBe('neutral');
    expect(verdictTone('unknown', null)).toBe('warning');
    expect(verdictTone('cancelled', null)).toBe('neutral');
  });

  it('классы только из токенов палитры', () => {
    for (const tone of ['success', 'error', 'warning', 'neutral'] as const) {
      const classes = toneClasses(tone);
      expect(classes).not.toMatch(/(gray|green|red|yellow|purple|blue)-\d/);
      expect(classes).toMatch(/(success|error|warning|dark)-/);
    }
  });
});
```

`unitSelection.test.ts`:

```ts
import { describe, expect, it } from 'vitest';
import type { Unit } from '@/api/reachability';
import { defaultDpiFor, filterUnits, loadSelection, regionsOf, saveSelection, toggleKey } from './unitSelection';

const unit = (op_key: string, dpi: 'on' | 'off', region_code: string, probeable = true): Unit => ({
  op_key, operator: op_key.split('|')[0], name: op_key, region: region_code.toUpperCase(), region_code, dpi, channel_state: dpi === 'on' ? 'DPI_ON' : 'DPI_OFF', probeable,
});
const UNITS = [unit('mts|цфо|off', 'off', 'cfo'), unit('mts|пфо|on', 'on', 'pfo'), unit('tele2|цфо|on', 'on', 'cfo'), unit('yota|уфо|off', 'off', 'urfo', false)];

describe('filterUnits', () => {
  it('фильтрует по режиму Белого списка и округу', () => {
    expect(filterUnits(UNITS, { dpi: 'on', region: null }).map((u) => u.op_key)).toEqual(['mts|пфо|on', 'tele2|цфо|on']);
    expect(filterUnits(UNITS, { dpi: 'any', region: 'cfo' }).map((u) => u.op_key)).toEqual(['mts|цфо|off', 'tele2|цфо|on']);
  });
});

describe('regionsOf / toggleKey / defaultDpiFor', () => {
  it('округа уникальны и подписаны', () => {
    expect(regionsOf(UNITS)).toEqual([{ code: 'cfo', label: 'CFO' }, { code: 'pfo', label: 'PFO' }, { code: 'urfo', label: 'URFO' }]);
  });
  it('toggleKey возвращает новый массив', () => {
    const selected = ['a'];
    expect(toggleKey(selected, 'b')).toEqual(['a', 'b']);
    expect(toggleKey(['a', 'b'], 'a')).toEqual(['b']);
    expect(selected).toEqual(['a']);
  });
  it('назначение задаёт режим по умолчанию', () => {
    expect(defaultDpiFor(['bs'])).toBe('on');
    expect(defaultDpiFor(['regular'])).toBe('off');
    expect(defaultDpiFor(['bs', 'regular'])).toBe('on');
    expect(defaultDpiFor([])).toBe('on');
  });
});

describe('память выбора', () => {
  it('переживает отсутствие localStorage', () => {
    saveSelection('probe', ['mts|пфо|on']);
    expect(loadSelection('probe')).toEqual(typeof localStorage === 'undefined' ? [] : ['mts|пфо|on']);
  });
});
```

- [ ] **Step 2: Реализация**

`deepLink.ts`:

```ts
export const TAB_KEYS = ['summary', 'probe', 'vless', 'scan', 'history'] as const;
export type TabKey = (typeof TAB_KEYS)[number];

export interface DeepLinkTarget {
  kind: 'host' | 'node';
  ref: string;
}

export interface DeepLink {
  tab: TabKey;
  targets: DeepLinkTarget[];
  userId: number | null;
  shortUuid: string | null;
}

const BASE = '/admin/reachability';

function isTab(value: string | null): value is TabKey {
  return (TAB_KEYS as readonly string[]).includes(value ?? '');
}

/** ?tab=&target=host:<uuid>&target=node:<uuid>&user=<id>&sub=<shortUuid>. Цель без tab открывает «Проверку». */
export function parseReachabilityDeepLink(params: URLSearchParams): DeepLink {
  const targets = params
    .getAll('target')
    .map((raw) => raw.split(':', 2))
    .filter((parts): parts is [string, string] => parts.length === 2 && (parts[0] === 'host' || parts[0] === 'node') && parts[1] !== '')
    .map(([kind, ref]) => ({ kind: kind as DeepLinkTarget['kind'], ref }));
  const userRaw = params.get('user');
  const userId = userRaw && /^\d+$/.test(userRaw) ? Number(userRaw) : null;
  const shortUuid = params.get('sub') || null;
  const tabParam = params.get('tab');
  const tab: TabKey = isTab(tabParam) ? tabParam : targets.length ? 'probe' : userId || shortUuid ? 'vless' : 'summary';
  return { tab, targets, userId, shortUuid };
}

export function buildReachabilityLink(input: Partial<DeepLink>): string {
  const params = new URLSearchParams();
  const tab = input.tab ?? (input.targets?.length ? 'probe' : input.userId || input.shortUuid ? 'vless' : 'summary');
  params.set('tab', tab);
  for (const target of input.targets ?? []) params.append('target', `${target.kind}:${target.ref}`);
  if (input.userId) params.set('user', String(input.userId));
  if (input.shortUuid) params.set('sub', input.shortUuid);
  return `${BASE}?${params.toString()}`;
}
```

`verdict.ts`:

```ts
import type { Verdict } from '@/api/reachability';

export type Tone = 'success' | 'error' | 'warning' | 'neutral';

/** Цвет ячейки — соответствие ожиданию. Без ожидания (null) — нейтрально, unknown — предупреждение. */
export function verdictTone(verdict: Verdict, matches: boolean | null): Tone {
  if (verdict === 'cancelled') return 'neutral';
  if (verdict === 'unknown') return 'warning';
  if (matches === true) return 'success';
  if (matches === false) return 'error';
  return 'neutral';
}

export function toneClasses(tone: Tone): string {
  switch (tone) {
    case 'success':
      return 'border-success-500/30 bg-success-500/15 text-success-400';
    case 'error':
      return 'border-error-500/30 bg-error-500/15 text-error-400';
    case 'warning':
      return 'border-warning-500/30 bg-warning-500/15 text-warning-400';
    default:
      return 'border-dark-700/60 bg-dark-800/60 text-dark-300';
  }
}

export function verdictLabelKey(verdict: Verdict): string {
  return `admin.reachability.verdict.${verdict}`;
}
```

`unitSelection.ts`:

```ts
import type { JobKind, Purpose, Unit } from '@/api/reachability';

export type DpiFilter = 'on' | 'off' | 'any';

export function filterUnits(units: Unit[], filter: { dpi: DpiFilter; region: string | null }): Unit[] {
  return units.filter(
    (unit) => (filter.dpi === 'any' || unit.dpi === filter.dpi) && (!filter.region || unit.region_code === filter.region),
  );
}

export function regionsOf(units: Unit[]): Array<{ code: string; label: string }> {
  const seen = new Map<string, string>();
  for (const unit of units) if (!seen.has(unit.region_code)) seen.set(unit.region_code, unit.region);
  return [...seen.entries()].map(([code, label]) => ({ code, label }));
}

export function toggleKey(selected: string[], key: string): string[] {
  return selected.includes(key) ? selected.filter((k) => k !== key) : [...selected, key];
}

export function defaultDpiFor(purposes: Purpose[]): DpiFilter {
  const distinct = new Set(purposes.filter((p) => p !== 'unknown'));
  if (distinct.size === 1 && distinct.has('regular')) return 'off';
  return 'on';
}

const STORAGE_PREFIX = 'cabinet_reachability_units_';

export function loadSelection(kind: JobKind): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + kind);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((k): k is string => typeof k === 'string') : [];
  } catch {
    return [];
  }
}

export function saveSelection(kind: JobKind, keys: string[]): void {
  try {
    localStorage.setItem(STORAGE_PREFIX + kind, JSON.stringify(keys));
  } catch {
    // хранилище недоступно — выбор просто не запомнится
  }
}
```

- [ ] **Step 3: Прогнать** — `npm run test -- reachability` → PASS.

- [ ] **Step 4: Коммит**

```bash
npm run lint && npm run format
git add src/components/admin/reachability/deepLink.ts src/components/admin/reachability/deepLink.test.ts src/components/admin/reachability/verdict.ts src/components/admin/reachability/verdict.test.ts src/components/admin/reachability/unitSelection.ts src/components/admin/reachability/unitSelection.test.ts
git commit -m "feat(reachability): ярлыки, цвета вердиктов и выбор симок — чистые модули"
```

---

### Task C4: Выбор симок и панель запуска

**Files:**
- Create: `src/components/admin/reachability/UnitPicker.tsx`, `LaunchPanel.tsx`, `useJobPreview.ts`

**Interfaces:**
- `UnitPicker({ kind, dpi, onDpiChange, selected, onChange })`: грузит `reachabilityApi.getUnits({ dpi: 'any' })` (react-query, ключ `['admin-reachability-units']`, `staleTime: 60_000`), чипы режима Белого списка, селект округа, группы по оператору, галочки; `selected: string[]` — `op_key`; при монтировании, если `selected` пуст — подставляет `loadSelection(kind)`; при изменении — `saveSelection`.
- `useJobPreview(body: JobCreateRequest | null)`: `useQuery` с ключом `['admin-reachability-preview', body]`, `enabled: !!body && body.targets.length > 0`, `staleTime: 0`, `retry: false`.
- `LaunchPanel({ kind, body, status, onStarted(job) })`: показывает preview (цена/оценка, пропуски, предупреждения, баланс), кнопку «Запустить за X» (`useMutation` → `reachabilityApi.createJob`), причины блокировки: нет целей, нет симок, дороже потолка, дороже баланса, занято (из `status.active_jobs` по `kind`), ошибка preview (текст через `getApiErrorMessage`).

- [ ] **Step 1: Реализация** `useJobPreview.ts`

```ts
import { useQuery } from '@tanstack/react-query';
import { reachabilityApi, type JobCreateRequest, type PreviewResponse } from '@/api/reachability';

export function useJobPreview(body: JobCreateRequest | null) {
  return useQuery<PreviewResponse>({
    queryKey: ['admin-reachability-preview', body],
    queryFn: () => reachabilityApi.previewJob(body as JobCreateRequest),
    enabled: Boolean(body && body.targets.length > 0),
    staleTime: 0,
    retry: false,
  });
}
```

`UnitPicker.tsx`:

```tsx
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { reachabilityApi, type JobKind, type Unit } from '@/api/reachability';
import { SkeletonGroup, Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';
import { type DpiFilter, filterUnits, loadSelection, regionsOf, saveSelection, toggleKey } from './unitSelection';

interface UnitPickerProps {
  kind: JobKind;
  dpi: DpiFilter;
  onDpiChange: (dpi: DpiFilter) => void;
  selected: string[];
  onChange: (keys: string[]) => void;
}

const DPI_OPTIONS: DpiFilter[] = ['on', 'off', 'any'];

export function UnitPicker({ kind, dpi, onDpiChange, selected, onChange }: UnitPickerProps) {
  const { t } = useTranslation();
  const [region, setRegion] = useState<string | null>(null);
  const { data: units = [], isLoading } = useQuery({
    queryKey: ['admin-reachability-units'],
    queryFn: () => reachabilityApi.getUnits({ dpi: 'any' }),
    staleTime: 60_000,
  });

  useEffect(() => {
    if (selected.length === 0) {
      const stored = loadSelection(kind).filter((key) => units.some((u) => u.op_key === key));
      if (stored.length) onChange(stored);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- только при загрузке списка
  }, [units.length]);

  const visible = useMemo(() => filterUnits(units, { dpi, region }), [units, dpi, region]);
  const grouped = useMemo(() => {
    const map = new Map<string, Unit[]>();
    for (const unit of visible) map.set(unit.name, [...(map.get(unit.name) ?? []), unit]);
    return [...map.entries()];
  }, [visible]);

  const update = (keys: string[]) => {
    onChange(keys);
    saveSelection(kind, keys);
  };

  if (isLoading) {
    return (
      <SkeletonGroup aria-label={t('admin.reachability.units.title')}>
        <Skeleton className="h-8 w-64 rounded-xl" />
        <Skeleton className="mt-3 h-32 w-full rounded-2xl" />
      </SkeletonGroup>
    );
  }
  if (units.length === 0) {
    return <p className="text-sm text-dark-400">{t('admin.reachability.units.empty')}</p>;
  }

  return (
    <section className="rounded-2xl border border-dark-700/60 bg-dark-800/60 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="mr-auto text-lg font-semibold text-dark-100">{t('admin.reachability.units.title')}</h2>
        {DPI_OPTIONS.map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => onDpiChange(option)}
            className={cn(
              'rounded-xl px-3 py-1.5 text-xs font-medium',
              dpi === option ? 'bg-accent-500 text-on-accent' : 'bg-dark-700 text-dark-300 hover:text-dark-100',
            )}
          >
            {t(`admin.reachability.units.dpi${option === 'on' ? 'On' : option === 'off' ? 'Off' : 'Any'}`)}
          </button>
        ))}
        <select
          value={region ?? ''}
          onChange={(event) => setRegion(event.target.value || null)}
          className="rounded-xl border border-dark-700 bg-dark-900 px-3 py-1.5 text-xs text-dark-100"
        >
          <option value="">{t('admin.reachability.units.allRegions')}</option>
          {regionsOf(units).map((r) => (
            <option key={r.code} value={r.code}>
              {r.label}
            </option>
          ))}
        </select>
      </div>

      <div className="mt-3 flex items-center gap-3 text-xs text-dark-400">
        <span>{t('admin.reachability.units.selected', { count: selected.length })}</span>
        <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={() => update([...new Set([...selected, ...visible.filter((u) => u.probeable).map((u) => u.op_key)])])}>
          {t('admin.reachability.units.selectAll')}
        </button>
        <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={() => update(selected.filter((k) => !visible.some((u) => u.op_key === k)))}>
          {t('admin.reachability.units.selectNone')}
        </button>
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {grouped.map(([operator, list]) => (
          <div key={operator} className="rounded-xl border border-dark-700/60 p-3">
            <p className="text-sm font-medium text-dark-100">{operator}</p>
            <ul className="mt-2 space-y-1">
              {list.map((unit) => (
                <li key={unit.op_key}>
                  <label className={cn('flex items-center gap-2 text-sm', unit.probeable ? 'text-dark-200' : 'text-dark-500')}>
                    <input
                      type="checkbox"
                      className="h-4 w-4 rounded border-dark-600 accent-accent-500"
                      checked={selected.includes(unit.op_key)}
                      disabled={!unit.probeable}
                      onChange={() => update(toggleKey(selected, unit.op_key))}
                    />
                    <span className="font-mono text-xs">{unit.region}</span>
                    <span className="text-xs text-dark-400">{unit.dpi === 'on' ? 'БС' : '—'}</span>
                    {!unit.probeable && <span className="text-xs text-warning-400">{t('admin.reachability.units.notProbeable')}</span>}
                  </label>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}
```

Подпись «БС» — сокращение из локали: вынести в ключ `admin.reachability.units.bsShort` («БС» / «WL») и использовать `t(...)`.

`LaunchPanel.tsx`:

```tsx
import { useTranslation } from 'react-i18next';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { reachabilityApi, type Job, type JobCreateRequest, type ReachabilityStatus } from '@/api/reachability';
import { getApiErrorMessage } from '@/utils/api-error';
import { Button } from '@/components/primitives';
import { formatKopeks } from './money';
import { useJobPreview } from './useJobPreview';
import { REACHABILITY_STATUS_KEY } from './useReachabilityStatus';

interface LaunchPanelProps {
  body: JobCreateRequest | null;
  status: ReachabilityStatus | undefined;
  onStarted: (job: Job) => void;
}

export function LaunchPanel({ body, status, onStarted }: LaunchPanelProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const preview = useJobPreview(body);
  const create = useMutation({
    mutationFn: (request: JobCreateRequest) => reachabilityApi.createJob(request),
    onSuccess: (job) => {
      queryClient.invalidateQueries({ queryKey: REACHABILITY_STATUS_KEY });
      onStarted(job);
    },
  });

  const busy = status?.active_jobs.find((job) => job.kind === body?.kind);
  const limit = status?.cost_limit_kopeks ?? 0;
  const cost = preview.data?.cost_kopeks ?? null;
  const balance = preview.data?.balance_kopeks ?? status?.balance_kopeks ?? null;

  let blocker: string | null = null;
  if (!body || body.targets.length === 0) blocker = t('admin.reachability.launch.noTargets');
  else if (busy) blocker = t('admin.reachability.launch.busy', { kind: t(`admin.reachability.kinds.${busy.kind}`), id: busy.id });
  else if (preview.isError) blocker = `${t('admin.reachability.launch.previewFailed')}: ${getApiErrorMessage(preview.error, '')}`;
  else if (preview.data && preview.data.units_resolved.length === 0) blocker = t('admin.reachability.launch.noUnits');
  else if (limit > 0 && cost !== null && cost > limit) blocker = t('admin.reachability.launch.overLimit', { limit: formatKopeks(limit) });
  else if (balance !== null && cost !== null && cost > balance) blocker = t('admin.reachability.launch.overBalance');

  const skipped = preview.data?.skipped;
  const names = (list: Array<{ op_key?: string }> | undefined) => (list ?? []).map((u) => u.op_key).filter(Boolean).join(', ');

  return (
    <section className="rounded-2xl border border-dark-700/60 bg-dark-800/60 p-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-xs uppercase tracking-wide text-dark-400">{t('admin.reachability.launch.price')}</p>
          <p className="text-2xl font-bold text-dark-50">{preview.isFetching ? '…' : formatKopeks(cost)}</p>
          {preview.data && !preview.data.estimate_is_exact && (
            <p className="text-xs text-warning-400">{t('admin.reachability.launch.estimate')}</p>
          )}
          <p className="mt-1 text-xs text-dark-400">
            {t('admin.reachability.launch.balance')}: {formatKopeks(balance)}
          </p>
        </div>
        <Button
          variant="primary"
          disabled={Boolean(blocker) || preview.isFetching || create.isPending || !body}
          onClick={() => body && create.mutate(body)}
        >
          {create.isPending ? t('admin.reachability.launch.running') : t('admin.reachability.launch.run', { price: formatKopeks(cost) })}
        </Button>
      </div>

      {blocker && <p className="mt-3 text-sm text-warning-400">{blocker}</p>}
      {create.isError && <p className="mt-3 text-sm text-error-400">{getApiErrorMessage(create.error, t('admin.reachability.progress.failed'))}</p>}

      {preview.data && (
        <ul className="mt-3 space-y-1 text-xs text-dark-400">
          {skipped && skipped.dpi_off.length > 0 && <li>{t('admin.reachability.launch.skippedDpiOff', { units: names(skipped.dpi_off) })}</li>}
          {skipped && skipped.unavailable.length > 0 && <li>{t('admin.reachability.launch.skippedUnavailable', { units: names(skipped.unavailable) })}</li>}
          {preview.data.warnings.map((warning) => (
            <li key={warning} className="text-warning-400">
              {warning}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
```

Проверить имя варианта у примитива `Button` (`src/components/primitives/Button/Button.variants.ts`): если `primary` не существует, взять действующий (например, `default`), либо использовать класс `.btn-primary` на `<button>`.

- [ ] **Step 2: Прогнать** — `npm run type-check && npm run lint`.

- [ ] **Step 3: Коммит**

```bash
npm run format
git add src/components/admin/reachability/UnitPicker.tsx src/components/admin/reachability/LaunchPanel.tsx src/components/admin/reachability/useJobPreview.ts src/locales
git commit -m "feat(reachability): выбор симок и панель запуска с ценой"
```

---

### Task C5: Цели и вкладки запуска

**Files:**
- Create: `HostsTargetList.tsx`, `NodesTargetList.tsx`, `SubscriptionConfigs.tsx`, `CustomTargetInput.tsx`, `CidrInput.tsx`, `ProbeTab.tsx`, `VlessTab.tsx`, `ScanTab.tsx` (все в `src/components/admin/reachability/`)

**Interfaces:**
- `HostsTargetList({ selected: string[]; onToggle(uuid); preselected: string[] })`: `getHosts(includeDisabled)`; поиск; переключатель назначения (`Switch` или три кнопки) → `updatePref` с инвалидацией `['admin-reachability-hosts']`; бейдж «догадка» при `purpose_guessed`; отключённые — по чекбоксу «показывать отключённые».
- `NodesTargetList({ selected, onToggle })`: `getNodes()`; подпись «только ping сервера»; число привязанных хостов.
- `CustomTargetInput({ values: string[]; onChange })`: поле + кнопка «Добавить», список чипов с удалением; валидация формы минимальная (непустая строка), настоящая — на сервере (400 с текстом).
- `CidrInput({ value: string; onChange; hosts?: HostTarget[] })`: поле + селект «подсеть этого хоста» (адрес хоста → бот сам резолвит; отправляем как `custom`? Нет: для скана нужен `/24` — селект подставляет `address/24`, и если адрес — домен, сервер вернёт 400 «не похоже на подсеть»; поэтому в селект попадают только хосты с IP-адресом (`/^\d+\.\d+\.\d+\.\d+$/`), значение `a.b.c.0/24`).
- `SubscriptionConfigs({ userId, shortUuid, selected: number[]; onToggle(index); onSource(next: {userId?; shortUuid?}) })`: грузит `getSubscriptionConfigs`; список конфигов с чекбоксами (не больше 20 выбранных), пропущенные с причинами; смена источника — поиск пользователя через `adminUsersApi.getUsers({ search, limit: 8 })` (как в `AdminBulkActions.tsx`) с дебаунсом 300 мс.
- `ProbeTab({ status, preselected })`, `VlessTab({ status, userId, shortUuid })`, `ScanTab({ status })`: собирают `JobCreateRequest`, рендерят выбор целей, `UnitPicker`, `LaunchPanel`, а после запуска — `JobProgress` (Task C6) с кнопкой «новая проверка».

Тело запроса в `ProbeTab`:

```ts
const body: JobCreateRequest | null = targets.length
  ? {
      kind: 'probe',
      targets: [
        ...hostUuids.map((ref) => ({ kind: 'host' as const, ref })),
        ...nodeUuids.map((ref) => ({ kind: 'node' as const, ref })),
        ...custom.map((value) => ({ kind: 'custom' as const, value })),
      ],
      units,
      dpi,
      probes: { icmp, tcp, sni },
      core: '',
    }
  : null;
```

Режим Белого списка по умолчанию — `defaultDpiFor(выбранные хосты → purpose)`; пробы по умолчанию `tcp` + `sni`, ICMP выключен; для нод принудительно `icmp: true` (если выбраны только ноды — `tcp/sni` можно выключить). VLESS: `targets = selected.map(index => ({ kind: 'subscription_config', short_uuid, index }))`, `core` из селекта. Скан: `targets = [{ kind: 'cidr', value: cidr }]`, пробы `icmp + tcp`, SNI по чекбоксу (тогда сервер возьмёт SNI из хостов, если добавить их целями; в v1 для скана SNI выключен по умолчанию).

- [ ] **Step 1: Реализация** — писать компоненты по интерфейсам выше в стиле `UnitPicker` (карточки `rounded-2xl`, заголовки `text-lg font-semibold text-dark-100`, списки с чекбоксами, `Skeleton` при загрузке, тексты только через `t`). Ключевые фрагменты:

`HostsTargetList` — переключатель назначения:

```tsx
const setPurpose = useMutation({
  mutationFn: (input: { uuid: string; purpose: Purpose }) =>
    reachabilityApi.updatePref({ target_kind: 'host', target_ref: input.uuid, purpose: input.purpose }),
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin-reachability-hosts'] }),
});
// в строке хоста:
<select value={host.purpose} onChange={(e) => setPurpose.mutate({ uuid: host.uuid, purpose: e.target.value as Purpose })} className="rounded-lg border border-dark-700 bg-dark-900 px-2 py-1 text-xs">
  {(['bs', 'regular', 'unknown'] as Purpose[]).map((p) => <option key={p} value={p}>{t(`admin.reachability.purpose.${p}`)}</option>)}
</select>
{host.purpose_guessed && <span className="text-xs text-dark-500">{t('admin.reachability.purpose.guessed')}</span>}
```

`SubscriptionConfigs` — лимит 20:

```tsx
const atLimit = selected.length >= 20;
<input type="checkbox" checked={selected.includes(cfg.index)} disabled={!selected.includes(cfg.index) && atLimit} onChange={() => onToggle(cfg.index)} />
{atLimit && <p className="text-xs text-warning-400">{t('admin.reachability.subscription.limit')}</p>}
```

`ProbeTab` — после запуска:

```tsx
const [jobId, setJobId] = useState<number | null>(null);
if (jobId !== null) return <JobProgress jobId={jobId} onReset={() => setJobId(null)} />;
```

- [ ] **Step 2: Прогнать** — `npm run type-check && npm run lint`; вручную открыть `/admin/reachability?tab=probe` в dev-сервере с ботом (или через браузерную обвязку с моками) и убедиться, что списки грузятся, цена считается, кнопка блокируется без целей.

- [ ] **Step 3: Коммит**

```bash
npm run format
git add src/components/admin/reachability
git commit -m "feat(reachability): выбор целей и вкладки запуска probe / VLESS / скан"
```

---

### Task C6: Прогресс задачи и результаты

**Files:**
- Create: `useReachabilityJob.ts`, `useReachabilityJob.test.tsx`, `JobProgress.tsx`, `JobResult.tsx`, `ProbeResult.tsx`, `VlessResult.tsx`, `ScanResult.tsx`

**Interfaces:**
- `useReachabilityJob(jobId: number | null, options?: { pollMs?: number; maxMs?: number })` → `{ job: Job | undefined; phase: 'idle' | 'loading' | 'running' | 'done' | 'failed' | 'cancelled' | 'stalled'; error: string | null; refetch(); }`. Опрос `pollMs` (по умолчанию 3000) пока `status` ∈ `pending|running`; через `maxMs` (по умолчанию 25 мин) опрос прекращается, `phase = 'stalled'`.
- `JobProgress({ jobId, onReset })`: стадии по `job.phase` с таймером от `job.started_at`, подсказка по виду, кнопки «Отменить» (`vless`/`scan`, статус активный), «Забрать результат» (`probe` в `retrieving` или `stalled`), после завершения — `JobResult`.
- `JobResult({ job })` → по `job.kind` рендерит `ProbeResult`/`VlessResult`/`ScanResult` + строку «Списано X (возврат Y)».

- [ ] **Step 1: Падающий тест хука** `useReachabilityJob.test.tsx`

```tsx
// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Job } from '@/api/reachability';

vi.mock('@/api/reachability', () => ({ reachabilityApi: { getJob: vi.fn() } }));

import { reachabilityApi } from '@/api/reachability';
import { useReachabilityJob } from './useReachabilityJob';

const job = (overrides: Partial<Job>): Job =>
  ({ id: 1, kind: 'probe', status: 'running', phase: 'waiting', trigger: 'manual', started_by_user_id: 1, external_id: null, targets: [], units_requested: [], units_resolved: [], units_effective: null, skipped: null, dpi: 'on', estimated_kopeks: 18, estimate_is_exact: true, cost_kopeks: null, refunded_kopeks: null, result: null, error_code: null, error_message: null, retryable: null, attempts: 1, created_at: null, started_at: new Date().toISOString(), finished_at: null, legs: [], ...overrides }) as Job;

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe('useReachabilityJob', () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it('опрашивает задачу, пока она идёт, и останавливается на done', async () => {
    const getJob = vi.mocked(reachabilityApi.getJob);
    getJob.mockResolvedValueOnce(job({ status: 'running' })).mockResolvedValueOnce(job({ status: 'done', phase: null, cost_kopeks: 18 }));

    const { result } = renderHook(() => useReachabilityJob(1, { pollMs: 50 }), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.phase).toBe('running'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(80);
    });
    await waitFor(() => expect(result.current.phase).toBe('done'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(getJob).toHaveBeenCalledTimes(2);
  });

  it('failed и cancelled — конечные стадии с текстом ошибки', async () => {
    vi.mocked(reachabilityApi.getJob).mockResolvedValue(job({ status: 'failed', error_code: 'no_dpi_on', error_message: 'нет симок' }));
    const { result } = renderHook(() => useReachabilityJob(2, { pollMs: 50 }), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.phase).toBe('failed'));
    expect(result.current.error).toBe('нет симок');
  });

  it('после maxMs опрос прекращается со стадией stalled', async () => {
    vi.mocked(reachabilityApi.getJob).mockResolvedValue(job({ status: 'running', phase: 'retrieving' }));
    const { result } = renderHook(() => useReachabilityJob(3, { pollMs: 50, maxMs: 120 }), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.phase).toBe('running'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    await waitFor(() => expect(result.current.phase).toBe('stalled'));
  });

  it('без jobId ничего не запрашивает', () => {
    const { result } = renderHook(() => useReachabilityJob(null), { wrapper: wrapper() });
    expect(result.current.phase).toBe('idle');
    expect(reachabilityApi.getJob).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Реализация хука** `useReachabilityJob.ts`

```ts
import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { reachabilityApi, type Job } from '@/api/reachability';
import { getApiErrorMessage } from '@/utils/api-error';

export type JobPhase = 'idle' | 'loading' | 'running' | 'done' | 'failed' | 'cancelled' | 'stalled';

const ACTIVE = new Set(['pending', 'running']);
const DEFAULT_POLL_MS = 3_000;
const DEFAULT_MAX_MS = 25 * 60_000;

/**
 * Опрос нашей задачи (не внешнего API): дёшево, поэтому раз в 3 с. Через maxMs
 * опрос прекращается — задача продолжает жить на сервере, результат будет в истории.
 */
export function useReachabilityJob(jobId: number | null, options: { pollMs?: number; maxMs?: number } = {}) {
  const pollMs = options.pollMs ?? DEFAULT_POLL_MS;
  const maxMs = options.maxMs ?? DEFAULT_MAX_MS;
  const startedAt = useRef(Date.now());
  const [stalled, setStalled] = useState(false);

  useEffect(() => {
    startedAt.current = Date.now();
    setStalled(false);
  }, [jobId]);

  const query = useQuery<Job>({
    queryKey: ['admin-reachability-job', jobId],
    queryFn: () => reachabilityApi.getJob(jobId as number),
    enabled: jobId !== null,
    gcTime: 0,
    retry: false,
    refetchInterval: (q) => {
      const data = q.state.data;
      if (data && !ACTIVE.has(data.status)) return false;
      if (Date.now() - startedAt.current > maxMs) {
        setStalled(true);
        return false;
      }
      return pollMs;
    },
  });

  const job = query.data;
  let phase: JobPhase = 'idle';
  if (jobId !== null) {
    if (!job) phase = query.isError ? 'failed' : 'loading';
    else if (job.status === 'done') phase = 'done';
    else if (job.status === 'failed') phase = 'failed';
    else if (job.status === 'cancelled') phase = 'cancelled';
    else phase = stalled ? 'stalled' : 'running';
  }

  const error = query.isError ? getApiErrorMessage(query.error, '') : job?.status === 'failed' ? job.error_message : null;
  return { job, phase, error, refetch: query.refetch };
}
```

- [ ] **Step 3: JobProgress и результаты**

`JobProgress.tsx`:

```tsx
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { reachabilityApi } from '@/api/reachability';
import { Button } from '@/components/primitives';
import { getApiErrorMessage } from '@/utils/api-error';
import { JobResult } from './JobResult';
import { useReachabilityJob } from './useReachabilityJob';
import { REACHABILITY_STATUS_KEY } from './useReachabilityStatus';

interface JobProgressProps {
  jobId: number;
  onReset: () => void;
}

function elapsedLabel(startedAt: string | null): string {
  if (!startedAt) return '0:00';
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

export function JobProgress({ jobId, onReset }: JobProgressProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { job, phase, error, refetch } = useReachabilityJob(jobId);
  const [, tick] = useState(0);

  useEffect(() => {
    if (phase !== 'running') return;
    const timer = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [phase]);

  useEffect(() => {
    if (phase === 'done' || phase === 'failed' || phase === 'cancelled') {
      queryClient.invalidateQueries({ queryKey: REACHABILITY_STATUS_KEY });
    }
  }, [phase, queryClient]);

  const cancel = useMutation({ mutationFn: () => reachabilityApi.cancelJob(jobId), onSuccess: () => refetch() });
  const retrieve = useMutation({ mutationFn: () => reachabilityApi.retrieveJob(jobId), onSuccess: () => refetch() });

  if (!job) return <p className="text-sm text-dark-400">…</p>;

  const stageKey = job.phase ?? 'submitting';
  const canCancel = job.kind !== 'probe' && (job.status === 'pending' || job.status === 'running') && job.phase !== 'cancelling';
  const canRetrieve = job.kind === 'probe' && (job.phase === 'retrieving' || phase === 'stalled');

  return (
    <section className="space-y-4 rounded-2xl border border-dark-700/60 bg-dark-800/60 p-4">
      {(phase === 'running' || phase === 'loading' || phase === 'stalled') && (
        <div>
          <p className="text-sm font-medium text-dark-100">
            {phase === 'stalled'
              ? t('admin.reachability.progress.stalled')
              : t(`admin.reachability.progress.${stageKey}`, { elapsed: elapsedLabel(job.started_at) })}
          </p>
          <p className="mt-1 text-xs text-dark-400">{t(`admin.reachability.progress.hint${job.kind === 'probe' ? 'Probe' : job.kind === 'vless' ? 'Vless' : 'Scan'}`)}</p>
          <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-dark-700">
            <div className="h-full w-1/3 animate-pulse rounded-full bg-accent-500" />
          </div>
          <div className="mt-3 flex gap-2">
            {canCancel && (
              <Button variant="secondary" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
                {t('admin.reachability.progress.cancel')}
              </Button>
            )}
            {canRetrieve && (
              <Button variant="secondary" onClick={() => retrieve.mutate()} disabled={retrieve.isPending}>
                {t('admin.reachability.progress.retrieve')}
              </Button>
            )}
          </div>
          {(cancel.isError || retrieve.isError) && (
            <p className="mt-2 text-sm text-error-400">{getApiErrorMessage(cancel.error ?? retrieve.error, '')}</p>
          )}
        </div>
      )}

      {phase === 'failed' && (
        <div className="rounded-xl border border-error-500/30 bg-error-500/10 p-3 text-sm text-dark-100">
          <p className="font-medium">{t('admin.reachability.progress.failed')}</p>
          <p className="mt-1 text-dark-300">
            {error} {job.error_code && <span className="font-mono text-xs text-dark-500">[{job.error_code}]</span>}
          </p>
        </div>
      )}

      {phase === 'cancelled' && <p className="text-sm text-dark-300">{t('admin.reachability.progress.cancelled')}</p>}

      {(phase === 'done' || phase === 'cancelled') && <JobResult job={job} />}

      {phase !== 'running' && phase !== 'loading' && (
        <Button variant="ghost" onClick={onReset}>
          {t('admin.reachability.progress.retry')}
        </Button>
      )}
    </section>
  );
}
```

`JobResult.tsx` — шапка «Списано» и выбор компонента результата:

```tsx
import { useTranslation } from 'react-i18next';
import type { Job } from '@/api/reachability';
import { formatKopeks } from './money';
import { ProbeResult } from './ProbeResult';
import { VlessResult } from './VlessResult';
import { ScanResult } from './ScanResult';

export function JobResult({ job }: { job: Job }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3">
      <p className="text-sm text-dark-300">
        {t('admin.reachability.result.cost')}: <span className="font-semibold text-dark-50">{formatKopeks(job.cost_kopeks)}</span>
        {job.refunded_kopeks ? ` (${t('admin.reachability.result.refunded', { amount: formatKopeks(job.refunded_kopeks) })})` : ''}
      </p>
      {job.kind === 'probe' && <ProbeResult job={job} />}
      {job.kind === 'vless' && <VlessResult job={job} />}
      {job.kind === 'scan' && <ScanResult job={job} />}
    </div>
  );
}
```

`ProbeResult` — таблица: строки `target_key` (из `job.legs`, порядок появления), столбцы `op_key`; ячейка — бейдж `toneClasses(verdictTone(...))` с `t(verdictLabelKey)`; клик по ячейке раскрывает `<pre>` с `JSON.stringify(leg.raw, null, 2)` в `rounded-xl bg-dark-900 p-3 text-xs`. Таблица внутри `overflow-x-auto`.

`VlessResult` — по легам: имя сервера, симка, вердикт, `tunnel_up`, `targets ok/total` из `leg.raw`, задержка `tcp_latency_ms`, `used_core`, `fail_reason`, `diagnosis` (строкой из `raw.diagnosis`).

`ScanResult` — из `job.result.status.result`: `up_n/total`, по симкам число живых, список IP с чекбоксом «показать по симке», кнопка «Скопировать список» (`navigator.clipboard.writeText(ips.join('\n'))`, состояние «Скопировано» 2 с). Никаких `<a download>`.

- [ ] **Step 4: Прогнать** — `npm run test -- useReachabilityJob && npm run type-check && npm run lint`.

- [ ] **Step 5: Коммит**

```bash
npm run format
git add src/components/admin/reachability
git commit -m "feat(reachability): прогресс задачи с опросом и результаты probe / VLESS / скана"
```

---

### Task C7: Сводка и история

**Files:**
- Create: `HostsSummaryMatrix.tsx`, `SummaryTab.tsx`, `JobsHistory.tsx`
- Modify: удалить ключ `admin.reachability.comingSoon` из четырёх локалей, если он был добавлен в C2

**Interfaces:**
- `SummaryTab({ status })`: чипы режима Белого списка (`on`/`off`/`any`), `getSummary(dpi)` (ключ `['admin-reachability-summary', dpi]`, `staleTime: 30_000`), пустое состояние со ссылкой на вкладку «Проверка», матрица.
- `HostsSummaryMatrix({ summary })`: строки — `rows` (подпись: `target_key` + бейдж назначения), столбцы — `summary.units` (`op_key`, заголовок `operator`/`region`), ячейка — бейдж вердикта с возрастом (`t('admin.reachability.summary.age', { age })`, возраст через `Intl.RelativeTimeFormat`), клик → `Link` на `/admin/reachability?tab=history&job=<id>`; контейнер `overflow-x-auto`, первая колонка `sticky left-0`.
- `JobsHistory()`: фильтры вид/статус, `listJobs({ kind, status, offset, limit: 20 })` с «Показать ещё» (`useInfiniteQuery` либо `offset` в состоянии), строки: id, вид, цели (первые две `target_key` + «+N»), число симок, списано, статус, время; клик — `Sheet` с `JobResult` и сырым JSON; параметр `?job=<id>` открывает панель сразу.

- [ ] **Step 1: Реализация** — по интерфейсам, стиль как в C4–C6. Фрагмент ячейки матрицы:

```tsx
const cell = row.cells[unit.op_key];
<td key={unit.op_key} className="p-1">
  {cell ? (
    <Link to={`/admin/reachability?tab=history&job=${cell.job_id}`} title={t('admin.reachability.summary.openJob')}
      className={cn('block rounded-lg border px-2 py-1 text-center text-xs', toneClasses(verdictTone(cell.verdict, cell.matches_expectation)))}>
      {t(verdictLabelKey(cell.verdict))}
      <span className="block text-[10px] opacity-70">{relativeAge(cell.checked_at, t)}</span>
    </Link>
  ) : (
    <span className="block rounded-lg border border-dashed border-dark-700 px-2 py-1 text-center text-xs text-dark-500">—</span>
  )}
</td>
```

- [ ] **Step 2: Прогнать** — `npm run type-check && npm run lint && npm run test`.

- [ ] **Step 3: Коммит**

```bash
npm run format
git add src/components/admin/reachability src/locales
git commit -m "feat(reachability): сводка хост × симка и история задач"
```

---

### Task C8: Ярлыки на карточке ноды и у подписки

**Files:**
- Modify: `src/pages/AdminRemnawave.tsx` (`NodeCard`, блок кнопок рядом с GeoCheck, ~строки 248–262)
- Modify: `src/components/admin/userDetail/SubscriptionTab.tsx` (новый необязательный проп `reachabilityLink?: string | null`, кнопка-ссылка в блоке действий подписки)
- Modify: `src/pages/AdminUserDetail.tsx` (вычислить ссылку и передать проп)

- [ ] **Step 1: Карточка ноды**

В `AdminRemnawave.tsx` импортировать `useNavigate` (уже есть), `usePermissionStore`, `useReachabilityAvailable`, `buildReachabilityLink`, `CellSignalIcon`. В `NodeCard` (компонент получает `node`): 

```tsx
const canReach = usePermissionStore((s) => s.hasPermission('reachability:run')) && useReachabilityAvailable();
// в блоке кнопок, перед GeoCheck:
{canReach && (
  <button
    type="button"
    onClick={(e) => { e.stopPropagation(); navigate(buildReachabilityLink({ targets: [{ kind: 'node', ref: node.uuid }] })); }}
    className="rounded-lg bg-dark-700 p-1.5 text-dark-300 transition-colors hover:bg-dark-600 hover:text-dark-100"
    title={t('admin.reachability.title')}
    aria-label={t('admin.reachability.title')}
  >
    <CellSignalIcon className="h-3.5 w-3.5" />
  </button>
)}
```

`useNavigate` внутри `NodeCard`: если `navigate` там недоступен — получить через `useNavigate()` в `NodeCard` (хук допустим в компоненте). Правило хуков: вызывать `usePermissionStore` и `useReachabilityAvailable` безусловно, объединять результат после.

- [ ] **Step 2: Вкладка подписки**

В `SubscriptionTabProps` добавить `reachabilityLink?: string | null;`. В блоке действий подписки (рядом с первой `btn-primary w-full`, ~строка 302) добавить:

```tsx
{props.reachabilityLink && (
  <Link to={props.reachabilityLink} className="btn-secondary w-full text-center">
    {t('admin.reachability.shortcuts.checkSubscription')}
  </Link>
)}
```

Ключ `admin.reachability.shortcuts.checkSubscription` = «Проверить через операторов РФ» / «Check via Russian operators» (+ zh/fa). В `AdminUserDetail.tsx`:

```tsx
const canReach = usePermissionStore((s) => s.hasPermission('reachability:run'));
const reachAvailable = useReachabilityAvailable();
const reachabilityLink = canReach && reachAvailable && userId ? buildReachabilityLink({ tab: 'vless', userId }) : null;
// <SubscriptionTab … reachabilityLink={reachabilityLink} />
```

- [ ] **Step 3: Прогнать** — `npm run type-check && npm run lint && npm run test`; в браузере: кнопка на ноде ведёт на `/admin/reachability?tab=probe&target=node%3A<uuid>` и нода подставлена; кнопка у подписки ведёт на VLESS-тест с конфигами пользователя.

- [ ] **Step 4: Коммит**

```bash
npm run format
git add src/pages/AdminRemnawave.tsx src/components/admin/userDetail/SubscriptionTab.tsx src/pages/AdminUserDetail.tsx src/locales
git commit -m "feat(reachability): ярлыки проверки на карточке ноды и у подписки пользователя"
```

---

### Task C9: Проверка перед сдачей

- [ ] `npm run type-check && npm run lint && npm run format:check && npm run test && npm run build` — всё зелёное.
- [ ] Рендер в браузере через обвязку с мок-JWT и моками эндпоинтов (см. память проекта `project_cabinet_browser_harness`): страницы `/admin/reachability` на всех пяти вкладках, состояние «не настроено», прогресс задачи, результат probe с 16 симками (горизонтальный скролл внутри таблицы, body не скроллится), сводка. Снимки: светлая тема, тёмная тема, Mini App-вьюпорт 390×844. Проверить, что ни один класс не использует стоковые цвета (`grep -rn "text-white\|gray-\|green-\|red-\|purple-" src/components/admin/reachability` пусто).
- [ ] Проверить контраст вторичного текста на светлой теме (память `project_cabinet_contrast_audit`): подписи `text-dark-400` на карточках читаемы; при необходимости поднять до `text-dark-300`.
- [ ] `git status` чист; ветка готова к PR `dev → main` вместе с ботом (релиз по `project_release_workflow`).
