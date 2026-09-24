import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Gamepad2, SearchX, Sparkles } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { GameCard } from '@/components/games/GameCard';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import {
  AVAILABLE_GAMES,
  GAME_CATEGORIES,
  type GameCategoryId,
  type GameDefinition,
} from '@/lib/gamesCatalog';
import { cn } from '@/lib/utils';
import { getGameSelection, type GameSelectEntry } from '@/services/api';

/** Games the server owns a session lifecycle for. Their visibility comes from
 * `GET /games/select`: hidden games are absent and never rendered. */
const SERVER_MANAGED_IDS = new Set(['wordle', 'anagram', 'couple-questionnaire']);

/**
 * Game selection.
 *
 * The grid is rendered entirely from the catalogue in src/lib/gamesCatalog: adding a game is
 * a data change, and the category chips derive their counts from the same list, so this page
 * never needs restructuring as the library grows. Availability comes from the shared
 * configuration, so a title an administrator switched off on the manage screen leaves this
 * grid -- and the chip counts -- without this page knowing where the switch lives. Play
 * navigates to the game's screen when the catalogue carries a route, and falls back to a
 * placeholder toast for titles whose screen does not exist yet.
 */

type CategoryFilter = 'all' | GameCategoryId;

export const GamesSelectPage = () => {
  const navigate = useNavigate();
  const [filter, setFilter] = useState<CategoryFilter>('all');
  const { isEnabled } = useGamesConfig();
  const [serverStatus, setServerStatus] = useState<Map<string, GameSelectEntry> | null>(null);

  useEffect(() => {
    let cancelled = false;
    getGameSelection()
      .then((response) => {
        if (!cancelled) {
          setServerStatus(new Map(response.games.map((entry) => [entry.game_id, entry])));
        }
      })
      .catch(() => {
        // The catalogue's local configuration is the fallback; a failed fetch
        // must not empty the grid when the server cannot be reached.
        if (!cancelled) setServerStatus(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const notifyPlay = (game: GameDefinition) => {
    const server = serverStatus?.get(game.id);
    if (server && !server.playable) {
      toast(server.message ?? `${game.name} is not available right now.`);
      return;
    }
    if (game.route) {
      navigate(game.route);
    } else {
      toast(`${game.name} opens here once its game screen lands 🎮`);
    }
  };

  const liveGames = useMemo(
    () =>
      AVAILABLE_GAMES.filter((game) => {
        if (SERVER_MANAGED_IDS.has(game.id) && serverStatus !== null) {
          // Absent from the server list means hidden: never rendered.
          return serverStatus.has(game.id);
        }
        return isEnabled(game.id);
      }),
    [isEnabled, serverStatus],
  );

  const counts = useMemo(() => {
    const map = new Map<CategoryFilter, number>([['all', liveGames.length]]);
    GAME_CATEGORIES.forEach((category) => {
      map.set(
        category.id,
        liveGames.filter((game) => game.category === category.id).length,
      );
    });
    return map;
  }, [liveGames]);

  const visibleGames = useMemo(
    () =>
      filter === 'all'
        ? liveGames
        : liveGames.filter((game) => game.category === filter),
    [filter, liveGames],
  );

  const chips: Array<{ id: CategoryFilter; label: string }> = [
    { id: 'all', label: 'All games' },
    ...GAME_CATEGORIES.map((category) => ({ id: category.id, label: category.label })),
  ];

  const activeCategoryLabel =
    filter === 'all'
      ? null
      : GAME_CATEGORIES.find((category) => category.id === filter)?.label ?? null;

  return (
    <div>
      <GamesPageHeader
        eyebrow="Games"
        title="Select a Game"
        description="Pick a title to play. New games drop into this list as they ship — the page itself never changes."
      />

      {/* Category chips */}
      <div className="no-scrollbar -mx-3 mb-5 flex gap-2 overflow-x-auto px-3 pb-1 sm:mx-0 sm:flex-wrap sm:px-0">
        {chips.map((chip) => {
          const count = counts.get(chip.id) ?? 0;
          const isActive = filter === chip.id;
          return (
            <button
              key={chip.id}
              type="button"
              aria-pressed={isActive}
              onClick={() => setFilter(chip.id)}
              className={cn(
                'inline-flex shrink-0 items-center gap-2 rounded-full border px-4 py-2 text-xs font-bold uppercase tracking-wider transition-all sm:text-sm',
                isActive
                  ? 'border-transparent bg-gradient-romantic text-white shadow-romantic'
                  : 'border-border/70 bg-card/60 text-muted-foreground hover:border-romantic/50 hover:text-foreground',
              )}
            >
              {chip.label}
              <span
                className={cn(
                  'rounded-full px-1.5 py-0.5 text-[10px] font-bold tabular-nums',
                  isActive ? 'bg-white/25 text-white' : 'bg-secondary text-muted-foreground',
                )}
              >
                {count}
              </span>
            </button>
          );
        })}
      </div>

      {visibleGames.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-border/70 px-6 py-14 text-center">
          <SearchX className="mx-auto h-8 w-8 text-muted-foreground" />
          <p className="mt-3 text-base font-bold">Nothing in this category yet</p>
          <p className="mx-auto mt-1 max-w-sm text-sm text-muted-foreground">
            {activeCategoryLabel} games haven&apos;t landed in the library — check back soon.
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-5 rounded-full"
            onClick={() => setFilter('all')}
          >
            <Gamepad2 />
            Show all games
          </Button>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {visibleGames.map((game) => (
            <GameCard key={game.id} game={game} onPlay={notifyPlay} />
          ))}
        </div>
      )}

      {/* Placeholder strip: makes it obvious the grid is meant to grow. */}
      <div className="mt-6 flex items-center justify-center gap-2 rounded-2xl border border-dashed border-border/70 px-4 py-5 text-center text-sm text-muted-foreground">
        <Sparkles className="h-4 w-4 shrink-0 text-romantic" />
        <span>More games land regularly — this list grows as they ship.</span>
      </div>
    </div>
  );
};

export default GamesSelectPage;
