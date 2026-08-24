import type { AccountBalance, Currency, Money } from "@/types/finance";

const moneyPattern = /^(-?)(\d+)(?:\.(\d{1,4}))?$/;

export interface DashboardPeriod {
  dateFrom: string;
  dateTo: string;
  label: string;
}

export interface BalanceTotal {
  accountsCount: number;
  currency: Currency;
  total: Money;
}

function moneyToUnits(value: Money): bigint {
  const match = moneyPattern.exec(value);
  if (!match) throw new Error("Invalid money value");
  const units = (BigInt(match[2]) * BigInt(10_000)) + BigInt((match[3] ?? "").padEnd(4, "0"));
  return match[1] === "-" ? -units : units;
}

function unitsToMoney(value: bigint): Money {
  const sign = value < BigInt(0) ? "-" : "";
  const absolute = value < BigInt(0) ? -value : value;
  return `${sign}${absolute / BigInt(10_000)}.${String(absolute % BigInt(10_000)).padStart(4, "0")}`;
}

function zonedParts(value: Date, timeZone: string): Record<string, number> {
  return Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      day: "2-digit", hour: "2-digit", hourCycle: "h23", minute: "2-digit", month: "2-digit", second: "2-digit", timeZone, year: "numeric",
    }).formatToParts(value).filter((part) => part.type !== "literal").map((part) => [part.type, Number(part.value)]),
  );
}

function zonedStartOfMonth(reference: Date, timeZone: string): Date {
  const current = zonedParts(reference, timeZone);
  const targetWallTime = Date.UTC(current.year, current.month - 1, 1);
  let candidate = new Date(targetWallTime);
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const represented = zonedParts(candidate, timeZone);
    const representedWallTime = Date.UTC(represented.year, represented.month - 1, represented.day, represented.hour, represented.minute, represented.second);
    candidate = new Date(candidate.getTime() + targetWallTime - representedWallTime);
  }
  return candidate;
}

export function currentMonthPeriod(reference: Date, timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone): DashboardPeriod {
  const start = zonedStartOfMonth(reference, timeZone);
  return {
    dateFrom: start.toISOString(),
    dateTo: reference.toISOString(),
    label: new Intl.DateTimeFormat("ru-RU", { month: "long", timeZone, year: "numeric" }).format(reference),
  };
}

export function financialSummaryUrl(period: DashboardPeriod): string {
  const params = new URLSearchParams({ date_from: period.dateFrom, date_to: period.dateTo });
  return `/api/v1/financial-summary?${params.toString()}`;
}

export function groupBalanceTotals(balances: AccountBalance[]): BalanceTotal[] {
  const grouped = new Map<Currency, { accountsCount: number; units: bigint }>();
  for (const balance of balances) {
    const current = grouped.get(balance.currency) ?? { accountsCount: 0, units: BigInt(0) };
    grouped.set(balance.currency, {
      accountsCount: current.accountsCount + 1,
      units: current.units + moneyToUnits(balance.balance),
    });
  }
  return Array.from(grouped, ([currency, value]) => ({
    accountsCount: value.accountsCount,
    currency,
    total: unitsToMoney(value.units),
  })).sort((left, right) => left.currency.localeCompare(right.currency));
}
