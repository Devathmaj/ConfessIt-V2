import { useCallback, useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { ArrowRight, CalendarClock, Star } from 'lucide-react';
import { toast } from 'sonner';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { describeApiError } from '@/lib/apiError';
import { AVAILABLE_GAMES, getGameById } from '@/lib/gamesCatalog';
import { getFeaturedGames, replaceFeaturedGame } from '@/services/api';

/** Today as UTC `YYYY-MM-DD`, the only calendar the spotlight speaks. */
const todayIso = () => new Date().toISOString().slice(0, 10);

const gameLabel = (gameId: string): string =>
  getGameById(gameId)?.name ?? gameId;

/**
 * The spotlight's front door on the manage overview: one big card that shows
 * today's pick, lets the admin choose one game, and sets it as featured for
 * today.
 *
 * This card owns no schedule: picking another date, clearing days, or
 * planning a week ahead lives on the dedicated schedule page
 * (`/admin/games/manage/featured`). When another game holds today, the admin
 * confirms the replacement first: the dialog names the displaced game and
 * states the scoring rule. Scores already earned keep their 2x and are never
 * recalculated -- replacing only changes which game earns 2x from now on.
 */
export const AdminFeaturedTodayCard = () => {
  const { logActivity } = useGamesConfig();

  const [featuredId, setFeaturedId] = useState<string | null>(null);
  const [featuredDate, setFeaturedDate] = useState<string>(todayIso);
  const [loading, setLoading] = useState(true);
  const [gameId, setGameId] = useState<string>(AVAILABLE_GAMES[0]?.id ?? 'wordle');
  const [saving, setSaving] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const all = await getFeaturedGames();
      const today = todayIso();
      const match = all.find((entry) => entry.game_date === today) ?? null;
      setFeaturedId(match?.game_id ?? null);
      setFeaturedDate(match?.game_date ?? today);
      setGameId(match?.game_id ?? AVAILABLE_GAMES[0]?.id ?? 'wordle');
      setError(null);
    } catch (fetchError) {
      setError(describeApiError(fetchError, 'Could not load today\u2019s spotlight.'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const pickedLabel = gameLabel(gameId);
  const otherLabel =
    featuredId !== null && featuredId !== gameId ? gameLabel(featuredId) : null;
  const needsConfirm = otherLabel !== null;

  const handleConfirm = async () => {
    setSaving(true);
    setError(null);
    try {
      const created = await replaceFeaturedGame(featuredDate, gameId);
      setFeaturedId(created.game_id);
      setFeaturedDate(created.game_date);
      toast.success(`${pickedLabel} is featured for ${created.game_date}.`);
      logActivity({
        kind: 'featured',
        gameId,
        message: `${pickedLabel} set as today's featured game`,
      });
      setConfirmOpen(false);
      await refresh();
    } catch (saveError) {
      setError(describeApiError(saveError, 'Could not set the featured game.'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Card className="[border-radius:var(--edge-card)] border-amber-500/40 bg-amber-500/5 backdrop-blur">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <CardTitle className="flex items-center gap-2 text-xl">
                <Star className="h-5 w-5 text-amber-500" />
                Featured spotlight
              </CardTitle>
              <CardDescription>
                One game holds today&apos;s spotlight and earns 2x score. Scores
                already earned keep their multiplier and are never recalculated.
              </CardDescription>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {featuredId !== null && !loading && <Badge>Today: {gameLabel(featuredId)}</Badge>}
              <Button asChild variant="outline" size="sm">
                <NavLink to="/admin/games/manage/featured">
                  <CalendarClock />
                  Full schedule
                  <ArrowRight />
                </NavLink>
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {loading ? (
            <p className="text-sm text-muted-foreground">Checking today&apos;s spotlight…</p>
          ) : (
            <div className="flex flex-wrap items-end gap-3">
              <div className="flex flex-col gap-2">
                <span id="featured-today-game-label" className="text-sm font-medium leading-none">
                  Game
                </span>
                <Select
                  value={gameId}
                  onValueChange={(next) => {
                    setGameId(next);
                    setError(null);
                  }}
                >
                  <SelectTrigger
                    aria-labelledby="featured-today-game-label"
                    className="w-56"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {AVAILABLE_GAMES.map((game) => (
                      <SelectItem key={game.id} value={game.id}>
                        {game.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <Button
                type="button"
                size="lg"
                disabled={saving}
                onClick={() => {
                  if (needsConfirm) setConfirmOpen(true);
                  else void handleConfirm();
                }}
              >
                <Star />
                {saving ? 'Featuring…' : 'Set as Featured for today'}
              </Button>
            </div>
          )}
          {!loading && featuredId === null && (
            <p className="text-sm text-muted-foreground">
              No game holds today&apos;s spotlight yet.
            </p>
          )}
          {error && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {error}
            </p>
          )}
        </CardContent>
      </Card>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Replace today&apos;s featured game?</AlertDialogTitle>
            <AlertDialogDescription>
              {otherLabel} is currently featured for {featuredDate}. Setting {pickedLabel} as
              featured will remove {otherLabel} from the spotlight and set {pickedLabel} as
              the new featured game. Scores already earned keep their 2x and are never
              recalculated — from now on {pickedLabel} earns 2x instead of {otherLabel}.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={saving}>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => void handleConfirm()} disabled={saving}>
              {saving ? 'Replacing…' : 'Replace featured'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
};

export default AdminFeaturedTodayCard;
