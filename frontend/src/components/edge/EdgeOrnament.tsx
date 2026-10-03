import { Flower, Heart, Mail, Sparkles } from 'lucide-react';
import { cn } from '@/lib/utils';

export type EdgeOrnamentKind =
  | 'seal'
  | 'flap'
  | 'tape'
  | 'ribbon'
  | 'postmark'
  | 'sticker'
  | 'brackets'
  | 'tail'
  | 'heart';

/**
 * Optional ornament shown only by the matching data-edge preset (see
 * edge-style.css). Shape/border only, existing tokens only, never intercepts
 * clicks and hidden from assistive tech. Parent needs .edge-has-ornament.
 */
export const EdgeOrnament = ({
  kind,
  className,
}: {
  kind: EdgeOrnamentKind;
  className?: string;
}) => {
  const icon =
    kind === 'seal' ? (
      <Flower className="h-5 w-5" strokeWidth={1.5} />
    ) : kind === 'heart' ? (
      <Heart className="h-4 w-4" strokeWidth={1.5} />
    ) : kind === 'sticker' ? (
      <Sparkles className="h-4 w-4" strokeWidth={1.5} />
    ) : kind === 'postmark' ? (
      <Mail className="h-5 w-5" strokeWidth={1.5} />
    ) : null;

  return (
    <span
      aria-hidden
      data-ornament={kind}
      className={cn('edge-ornament', className)}
    >
      {icon}
    </span>
  );
};

/** Optional divider for section gaps. 1px existing-token lines + heart. */
export const HeartDivider = ({ className }: { className?: string }) => (
  <div
    aria-hidden
    className={cn('flex items-center gap-3', className)}
  >
    <span className="h-px flex-1 bg-border" />
    <Heart className="h-4 w-4" strokeWidth={1.5} />
    <span className="h-px flex-1 bg-border" />
  </div>
);

export default EdgeOrnament;
