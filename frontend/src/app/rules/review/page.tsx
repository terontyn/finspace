import { FinanceApp } from "@/components/finance-app";
import { parseCategorizationReviewImportScope } from "@/lib/categorization-review-scope";

export default async function RulesReviewPage({
  searchParams,
}: {
  searchParams: Promise<{ import_batch_id?: string | string[] }>;
}) {
  const params = await searchParams;
  return (
    <FinanceApp
      categorizationReviewScope={parseCategorizationReviewImportScope(params.import_batch_id)}
      screen="rules-review"
    />
  );
}
