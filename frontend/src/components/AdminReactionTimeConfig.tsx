import { useCallback, useEffect, useState } from 'react';
import { Timer, X } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { AdminGameStatusControls } from '@/components/AdminGameStatusControls';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { describeApiError } from '@/lib/apiError';
import {
  createReactionTimeGame,
  getAdminReactionTimeGame,
  getAdminReactionTimeGames,
  getCurrentAdminReactionTimeSession,
  type ReactionTimeGame,
} from '@/services/api';

/** Today as the server numbers it, in UTC, which is also what the date field starts on. */
const todayIso = () => new Date().toISOString().slice(0, 10);

/**
 * Featuring the Reaction Time game for one day.
 *
 * One control in: the day. There is deliberately no mode toggle, no timing
 * field, no difficulty, no attempt budget and no preview -- the mechanics are
 * fixed by the application (five submissions per user per session, a random
 * red wait, a client-measured reaction after green), so featuring a day
 * stores the day and nothing else.
 *
 * Featuring only pins the spotlight: sessions roll on the game's own cadence
 * with a fresh five submissions per player each period, and there is no
 * Update action for this game. The list below is the featured record. One day
 * holds one featured game, so a day that is already featured comes back from
 * the server as a 409 naming the date. That is shown inline rather than as a
 * toast: a toast disappears and the field is still full of a day that cannot
 * be saved, whereas the reason to stop is worth staying on screen until the
 * operator changes the day.
 */
export const AdminReactionTimeConfig = () => {
  const { logActivity } = useGamesConfig();

  const [games, setGames] = useState<ReactionTimeGame[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [gameDate, setGameDate] = useState<string>(todayIso);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [detail, setDetail] = useState<ReactionTimeGame | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [session, setSession] = useState<{
    id: string;
    period_start_date: string | null;
    period_end_date: string | null;
  } | null>(null);

  const refresh = useCallback(async () => {
    try {
      setGames(await getAdminReactionTimeGames());
      setListError(null);
    } catch (error) {
      setListError(describeApiError(error, 'Could not load the featured days.'));
    } finally {
      setLoading(false);
    }
    try {
      setSession(await getCurrentAdminReactionTimeSession());
    } catch {
      setSession(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleSave = async () => {
    setSaving(true);
    setFormError(null);
    try {
      const created = await createReactionTimeGame({ game_date: gameDate });

      toast.success(`Reaction Time featured for ${created.game_date}.`);
      logActivity({
        kind: 'puzzle',
        gameId: 'reaction-time',
        message: `Reaction Time for ${created.game_date} featured`,
      });
      setDetail(null);
      setDetailError(null);
      await refresh();
    } catch (error) {
      setFormError(describeApiError(error, 'Could not save the featured day.'));
    } finally {
      setSaving(false);
    }
  };

  const openDetail = async (gameId: string) => {
    setDetail(null);
    setDetailError(null);
    try {
      setDetail(await getAdminReactionTimeGame(gameId));
    } catch (error) {
      setDetailError(describeApiError(error, 'Could not load that featured day.'));
    }
  };

  const hasDate = gameDate.trim().length > 0;

  return (
    <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-xl">
              <Timer className="h-5 w-5" />
              Reaction Time
            </CardTitle>
            <CardDescription>
              One featured day at a time, and featuring never touches a session:
              play rolls on its own cadence with five fresh submissions per
              player each period, and there is no Update action for this game.
            </CardDescription>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        <AdminGameStatusControls gameId="reaction-time" gameName="Reaction Time" />

        {session && (
          <p className="rounded-xl border border-border/60 bg-muted/40 p-3 text-sm">
            Current session period:{' '}
            <span className="font-mono font-semibold">
              {session.period_start_date ?? '—'}
              {session.period_end_date ? ` → ${session.period_end_date}` : ''}
            </span>{' '}
            <span className="text-muted-foreground">
              — every player gets five fresh submissions each period.
            </span>
          </p>
        )}

        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-2">
              <Label htmlFor="reaction-date">Day</Label>
              <Input
                id="reaction-date"
                type="date"
                value={gameDate}
                onChange={(event) => {
                  setGameDate(event.target.value);
                  setFormError(null);
                }}
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <Button type="button" onClick={handleSave} disabled={!hasDate || saving}>
              {saving ? 'Saving...' : 'Save featured day'}
            </Button>
            <p className="text-xs text-muted-foreground">
              Featuring a day stores the day itself. Reaction timing stays fixed.
            </p>
          </div>

          {formError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {formError}
            </p>
          )}
        </div>

        <div className="flex flex-col gap-3 border-t border-border/40 pt-4">
          <div className="flex items-baseline justify-between gap-3">
            <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              Featured days
            </h4>
            <span className="text-xs text-muted-foreground">
              {loading ? 'Loading...' : `${games.length} featured`}
            </span>
          </div>

          {listError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {listError}
            </p>
          )}

          {!loading && !listError && games.length === 0 && (
            <p className="text-sm text-muted-foreground">
              Nothing featured yet. Pick the day above and it appears here.
            </p>
          )}

          {games.length > 0 && (
            <ul className="flex flex-col gap-2">
              {games.map((game) => (
                <li key={game.id}>
                  <div className="flex items-center justify-between gap-3 rounded-xl border border-border/60 p-3">
                    <button
                      type="button"
                      onClick={() => void openDetail(game.id)}
                      aria-label={`Inspect the featured day for ${game.game_date}`}
                      className="flex min-w-0 flex-1 flex-wrap items-center gap-3 text-left"
                    >
                      <span className="font-mono text-sm font-semibold">{game.game_date}</span>
                      <span className="text-xs text-muted-foreground">
                        Featured by {game.created_by}
                      </span>
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {detail && (
            <div className="rounded-xl border border-border/60 bg-muted/40 p-4 text-sm">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 space-y-1">
                  <p className="font-semibold">Featured day {detail.game_date}</p>
                  <p className="break-all font-mono text-xs text-muted-foreground">
                    {detail.id}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Featured by {detail.created_by} on{' '}
                    {new Date(detail.created_at).toLocaleString()} · updated{' '}
                    {new Date(detail.updated_at).toLocaleString()}
                  </p>
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setDetail(null)}
                  aria-label="Close"
                >
                  <X />
                </Button>
              </div>
            </div>
          )}

          {detailError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {detailError}
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
};

export default AdminReactionTimeConfig;
