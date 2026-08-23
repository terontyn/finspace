interface WorkspaceSwitcherProps {
  name: string;
}

export function WorkspaceSwitcher({ name }: WorkspaceSwitcherProps) {
  return (
    <button className="workspace-switcher" type="button" title="Текущее пространство">
      <span aria-hidden="true">{name.slice(0, 1).toUpperCase()}</span>
      <strong>{name}</strong>
      <svg aria-hidden="true" fill="none" height="14" viewBox="0 0 24 24" width="14"><path d="m7 9 5 5 5-5" /></svg>
    </button>
  );
}
