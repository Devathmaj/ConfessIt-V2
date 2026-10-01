import type { LucideIcon } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';

/**
 * Compact metric tile used by the Games dashboard stat strip. Kept intentionally generic --
 * label, value, hint, icon -- so new metrics can be added without touching the layout.
 */
interface StatCardProps {
  label: string;
  value: string | number;
  hint?: string;
  icon: LucideIcon;
  /** Tailwind gradient class for the icon tile. */
  accent?: string;
}

export const StatCard = ({ label, value, hint, icon: Icon, accent }: StatCardProps) => (
  <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
    <CardContent className="p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs font-bold uppercase tracking-wider text-muted-foreground">
            {label}
          </p>
          <p className="mt-1.5 truncate text-2xl font-extrabold tabular-nums sm:text-3xl">
            {value}
          </p>
          {hint && <p className="mt-1 truncate text-xs text-muted-foreground">{hint}</p>}
        </div>
        <span
          className={cn(
            'shrink-0 rounded-xl p-2.5 text-white',
            accent ?? 'bg-gradient-romantic',
          )}
        >
          <Icon className="h-5 w-5" />
        </span>
      </div>
    </CardContent>
  </Card>
);

export default StatCard;
