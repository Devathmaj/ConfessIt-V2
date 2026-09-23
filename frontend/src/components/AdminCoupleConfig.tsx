import { useCallback, useEffect, useState } from 'react';
import { HeartHandshake, X } from 'lucide-react';
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
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { describeApiError } from '@/lib/apiError';
import {
  createCoupleGame,
  getAdminCoupleGame,
  getAdminCoupleGames,
  type CoupleGame,
} from '@/services/api';

/** Today as the server numbers it, in UTC, which is also what the date field starts on. */
const todayIso = () => new Date().toISOString().slice(0, 10);

/**
 * Featuring the Couple Prediction game for one day.
 *
 * One control in: the day. There is deliberately no mode toggle, no word field,
 * no draw and no preview -- unlike Wordle and Anagram there is nothing per-day
 * for an administrator to type or draw. The 50-question bank is shared
 * application content seeded at startup, so featuring a day stores the day and
 * nothing else.
 *
 * The list below is the whole record. One day holds one featured instance, so a
 * day that is already featured comes back from the server as a 409 naming the
 * date. That is shown inline rather than as a toast: a toast disappears and the
 * field is still full of a day that cannot be saved, whereas the reason to stop
 * is worth staying on screen until the operator changes the day.
 */
export const AdminCoupleConfig = () => {
  const { logActivity } = useGamesConfig();

  const [games, setGames] = useState<CoupleGame[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [gameDate, setGameDate] = useState<string>(todayIso);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [detail, setDetail] = useState<CoupleGame | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setGames(await getAdminCoupleGames());
      setListError(null);
    } catch (error) {
      setListError(describeApiError(error, 'Could not load the featured days.'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleSave = async () => {
    setSaving(true);
    setFormError(null);
    try {
      const created = await createCoupleGame({ game_date: gameDate });

      toast.success(`Couple game featured for ${created.game_date}.`);
      logActivity({
        kind: 'puzzle',
        gameId: 'couple',
        message: `Couple game for ${created.game_date} featured`,
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
      setDetail(await getAdminCoupleGame(gameId));
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
              <HeartHandshake className="h-5 w-5" />
              Couple Prediction
            </CardTitle>
            <CardDescription>
              One featured day at a time. The 50 questions are shared for every day,
              so there is nothing to type -- pick the day and save it.
            </CardDescription>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-2">
              <Label htmlFor="couple-date">Day</Label>
              <Input
                id="couple-date"
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
              Featuring a day stores the day itself. The questions stay shared.
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

export default AdminCoupleConfig;
