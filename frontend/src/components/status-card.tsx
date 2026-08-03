import type { CheckStatus } from "@/types/system";

interface StatusCardProps {
  label: string;
  status: CheckStatus;
  description: string;
}

const statusLabels: Record<CheckStatus, string> = {
  checking: "Проверка",
  ok: "Работает",
  unavailable: "Недоступен",
};

export function StatusCard({ label, status, description }: StatusCardProps) {
  return (
    <article className="status-card">
      <div className="status-card__heading">
        <h2>{label}</h2>
        <span className={`status-badge status-badge--${status}`}>
          <span aria-hidden="true" className="status-dot" />
          {statusLabels[status]}
        </span>
      </div>
      <p>{description}</p>
    </article>
  );
}
