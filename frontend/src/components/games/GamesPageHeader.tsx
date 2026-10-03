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
  // Relative + z-10: section florals (GamesLayout + per-page pairs) are absolute
  // with z-index -1 and would otherwise sit directly behind this copy.
  <div className="relative z-10 mb-6 flex flex-col gap-4 sm:mb-8 sm:flex-row sm:items-end sm:justify-between">
    <div className="min-w-0">
      <p className="type-eyebrow header-halo text-romantic">{eyebrow}</p>
      <h1 className="header-halo mt-1.5 break-words text-2xl tracking-tight sm:text-3xl lg:text-4xl">
        {title}
      </h1>
      {description && (
        <p className="page-subtitle mt-2 max-w-2xl text-sm leading-relaxed sm:text-base">
          {description}
        </p>
      )}
    </div>
    {actions && <div className="flex shrink-0 flex-wrap gap-2">{actions}</div>}
  </div>
);

export default GamesPageHeader;
