import { FinanceApp } from "@/components/finance-app";

export default async function TransactionsPage({ searchParams }: { searchParams: Promise<{ account_id?: string; new?: string }> }) {
  const params = await searchParams;
  return <FinanceApp initialTransactionAccountId={params.account_id} openTransactionForm={params.new === "1"} screen="transactions" />;
}
