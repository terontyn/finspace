interface ComingSoonScreenProps {
  description: string;
  requiresApi?: boolean;
  title: string;
}

export function ComingSoonScreen({ description, requiresApi = false, title }: ComingSoonScreenProps) {
  return (
    <section>
      <header className="screen-header"><div><span className="kicker">Следующий этап продукта</span><h1>{title}</h1><p>{description}</p></div><span className="feature-badge">В разработке</span></header>
      <div className="panel feature-placeholder"><span className="feature-placeholder-icon" aria-hidden="true">•••</span><h2>Раздел пока не подключён к production</h2><p>Здесь не используются демонстрационные суммы или фиктивные операции. Экран будет включён после проектирования доменной модели и проверки API-контрактов.</p>{requiresApi ? <strong>Requires API support</strong> : null}</div>
    </section>
  );
}
