import type { Currency, Money } from "@/types/finance";

const moneyPattern = /^-?\d+(?:\.\d{1,4})?$/;

export function formatMoney(value: Money, currency: Currency): string {
  if (!moneyPattern.test(value)) {
    return `${value} ${currency}`;
  }

  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(Number(value));
}

export function moneyTone(value: Money): "positive" | "negative" | "neutral" {
  if (value.startsWith("-")) return "negative";
  if (/^0(?:\.0+)?$/.test(value)) return "neutral";
  return "positive";
}
