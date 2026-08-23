import { FinanceApp } from "@/components/finance-app";

export default async function AccountDetailsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <FinanceApp accountId={id} screen="accounts" />;
}
