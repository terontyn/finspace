import type { Currency } from "@/types/finance";

export type ReportCurrencyFilter = Currency | "ALL";

const monthPattern = /^(\d{4})-(0[1-9]|1[0-2])$/;

export function currentReportMonth(reference: Date, timezone: string): string {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      month: "2-digit",
      timeZone: timezone,
      year: "numeric",
    }).formatToParts(reference).map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}`;
}

export function reportPeriodFromMonth(month: string): { dateFrom: string; dateTo: string } {
  const match = monthPattern.exec(month);
  if (!match) throw new Error("Invalid report month");
  const year = Number(match[1]);
  const monthNumber = Number(match[2]);
  const lastDay = new Date(Date.UTC(year, monthNumber, 0)).getUTCDate();
  return {
    dateFrom: `${month}-01`,
    dateTo: `${month}-${String(lastDay).padStart(2, "0")}`,
  };
}

export function financialReportUrl(
  month: string,
  currency: ReportCurrencyFilter,
): string {
  const period = reportPeriodFromMonth(month);
  const params = new URLSearchParams({
    date_from: period.dateFrom,
    date_to: period.dateTo,
  });
  if (currency !== "ALL") params.set("currency", currency);
  return `/api/v1/reports/financial?${params.toString()}`;
}

export function parseReportQuery(
  search: string,
  fallbackMonth: string,
): { month: string; currency: ReportCurrencyFilter } {
  const params = new URLSearchParams(search);
  const period = params.get("period") ?? "";
  const currency = params.get("currency") ?? "ALL";
  return {
    month: monthPattern.test(period) ? period : fallbackMonth,
    currency: currency === "RUB" || currency === "EUR" || currency === "USD"
      ? currency
      : "ALL",
  };
}
