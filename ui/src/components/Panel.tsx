import type { ReactNode } from "react";

interface Props {
  title: string;
  id?: string;
  className?: string;
  actions?: ReactNode;
  children: ReactNode;
}

/** HUD panel: hairline border, corner brackets on hover/focus. */
export function Panel({ title, id, className, actions, children }: Props) {
  return (
    <section className={`panel${className ? ` ${className}` : ""}`} id={id} aria-label={title}>
      <header className="panel-head">
        <h2 className="hud-title">{title}</h2>
        {actions}
      </header>
      {children}
    </section>
  );
}
