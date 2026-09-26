import { Gamepad2 } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { categoryLabel, type GameDefinition } from '@/lib/gamesCatalog';
import { cn } from '@/lib/utils';

/**
 * The one card used everywhere a game is offered: the dashboard's quick-access grid and the
 * selection page grid. It reads only from a GameDefinition, so a new game added to the
 * catalogue appears anywhere this card is used without further UI work.
 */
interface GameCardProps {
  game: GameDefinition;
  /** Omitted where no play action applies. */
  onPlay?: (game: GameDefinition) => void;
  playLabel?: string;
  className?: string;
}

export const GameCard = ({ game, onPlay, playLabel, className }: GameCardProps) => {
  const Icon = game.icon;

  return (
    <Card
      className={cn(
        'group flex flex-col rounded-2xl border-border/60 bg-card/70 backdrop-blur transition-all hover:-translate-y-1 hover:border-romantic/50 hover:shadow-romantic',
        className,
      )}
    >
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <span
            className={cn(
              'flex h-12 w-12 items-center justify-center rounded-xl text-white transition-transform duration-300 group-hover:scale-105 sm:h-14 sm:w-14',
              game.accent,
            )}
          >
            <Icon className="h-6 w-6 sm:h-7 sm:w-7" />
          </span>
          <Badge variant="secondary" className="shrink-0 capitalize">
            {categoryLabel(game.category)}
          </Badge>
        </div>
        <CardTitle className="mt-3 text-lg font-bold leading-snug">{game.name}</CardTitle>
        <CardDescription className="text-sm leading-relaxed">{game.tagline}</CardDescription>
      </CardHeader>

      <CardContent className="mt-auto">
        <div className="mb-4 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <span className="font-semibold capitalize text-foreground/70">{game.difficulty}</span>
          <span aria-hidden>·</span>
          <span>{game.players}</span>
          <span aria-hidden>·</span>
          <span>{game.duration}</span>
        </div>
        <Button
          type="button"
          size="sm"
          variant="default"
          className="w-full rounded-full font-semibold"
          onClick={() => onPlay?.(game)}
        >
          <Gamepad2 />
          {playLabel ?? 'Play now'}
        </Button>
      </CardContent>
    </Card>
  );
};

export default GameCard;
