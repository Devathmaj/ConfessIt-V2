import type { ReactNode } from 'react';

/**
 * Shared page header for the Games section: a short eyebrow, a title and an optional
 * description/actions row. Every Games page opens with this so the section keeps one
 * hierarchy and spacing rhythm no matter which tab the user is on.
 */
interface GamesPageHeaderProps {
  eyebrow: string;
  title: ReactNode;
  description?: string;
  actions?: ReactNode;
}

export const GamesPageHeader = ({ eyebrow, title, description, actions }: GamesPageHeaderProps) => (
  <div className="mb-6 flex flex-col gap-4 sm:mb-8 sm:flex-row sm:items-end sm:justify-between">
    <div className="min-w-0">
      <p className="text-xs font-bold uppercase tracking-[0.2em] text-romantic">{eyebrow}</p>
      <h1 className="mt-1.5 break-words text-2xl font-extrabold tracking-tight sm:text-3xl lg:text-4xl">
        {title}
      </h1>
      {description && (
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-muted-foreground sm:text-base">
          {description}
        </p>
      )}
    </div>
    {actions && <div className="flex shrink-0 flex-wrap gap-2">{actions}</div>}
  </div>
);

export default GamesPageHeader;
