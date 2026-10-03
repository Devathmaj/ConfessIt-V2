import { useCallback, useEffect, useMemo, useState } from 'react';
import { CalendarClock, Star, X } from 'lucide-react';
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
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { describeApiError } from '@/lib/apiError';
import {
  clearFeaturedGame,
  getFeaturedGames,
  replaceFeaturedGame,
  setFeaturedGame,
  type FeaturedGame,
} from '@/services/api';

const FEATURED_IDS = [
  'wordle',
  'anagram',
  'couple-questionnaire',
  'reaction-time',
  'connections',
  'trivia-drop',
  'two-key-escape',
  'plot-twist',
  'case-closed',
] as const;

const GAME_LABELS: Record<string, string> = {
  wordle: 'Wordle',
  anagram: 'Anagram',
  'couple-questionnaire': 'Couple Questionnaire',
  'reaction-time': 'Reaction Time',
  connections: 'Connections',
  'trivia-drop': 'Trivia Drop',
  'two-key-escape': 'Two-Key Escape',
  'plot-twist': 'Plot Twist',
  'case-closed': 'Case Closed',
};

/** Today as UTC `YYYY-MM-DD`, the only calendar the schedule speaks. */
const todayIso = () => new Date().toISOString().slice(0, 10);

/** `YYYY-MM-DD` shifted by `days`, staying on the UTC calendar. */
const addDays = (iso: string, days: number): string => {
  const date = new Date(`${iso}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
};

/**
 * Consecutive-day runs per date: `game_date` to `{ run, pos }`, where `run`
 * is the whole unbroken run of that game and `pos` the date's place in it.
 * Runs surface the one-week cap in the list itself -- a 7-day run cannot take
 * another bordering day.
 */
const runsByDate = (entries: FeaturedGame[]): Map<string, { run: number; pos: number }> => {
  const sorted = [...entries].sort((a, b) => (a.game_date < b.game_date ? -1 : 1));
  const runs = new Map<string, { run: number; pos: number }>();
  let start = 0;
  while (start < sorted.length) {
    let end = start;
    while (
      end + 1 < sorted.length &&
      sorted[end + 1].game_id === sorted[start].game_id &&
      addDays(sorted[end].game_date, 1) === sorted[end + 1].game_date
    ) {
      end += 1;
    }
    for (let index = start; index <= end; index += 1) {
      runs.set(sorted[index].game_date, { run: end - start + 1, pos: index - start + 1 });
    }
    start = end + 1;
  }
  return runs;
};

/**
 * The spotlight schedule, owned by the server.
 *
 * One game per UTC date, enforced by the database as well as this form: two
 * games on one day race a unique constraint and one of them is refused. The
 * same game may hold many dates. Featuring never creates or touches a session
 * -- a non-featured game keeps its own sessions and stays playable.
 *
 * Any pin may erase the pick holding its date: when the chosen date already
 * belongs to another game, the admin confirms against that exact date first
 * and the replacement lands in one transaction.
 */
export const AdminFeaturedSchedule = () => {
  const [featured, setFeatured] = useState<FeaturedGame[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [gameDate, setGameDate] = useState<string>(todayIso);
  const [gameId, setGameId] = useState<string>('wordle');
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [confirmHolder, setConfirmHolder] = useState<FeaturedGame | null>(null);

  const refresh = useCallback(async () => {
    try {
      setFeatured(await getFeaturedGames());
      setListError(null);
    } catch (error) {
      setListError(describeApiError(error, 'Could not load the featured schedule.'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleSave = async () => {
    if (!gameDate) return;
    const holder = featured.find((entry) => entry.game_date === gameDate) ?? null;
    if (holder !== null && holder.game_id !== gameId) {
      // Another game's pick would be erased: confirm against that exact date.
      setConfirmHolder(holder);
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      const created = await setFeaturedGame(gameDate, gameId);
      toast.success(
        `${GAME_LABELS[created.game_id] ?? created.game_id} featured for ${created.game_date}.`,
      );
      await refresh();
    } catch (error) {
      setFormError(describeApiError(error, 'Could not save the featured game.'));
    } finally {
      setSaving(false);
    }
  };

  const handleConfirmReplace = async () => {
    if (confirmHolder === null || !gameDate) return;
    setSaving(true);
    setFormError(null);
    try {
      const replaced = await replaceFeaturedGame(gameDate, gameId);
      toast.success(
        `${GAME_LABELS[replaced.game_id] ?? replaced.game_id} featured for ${replaced.game_date} (replacing ${GAME_LABELS[confirmHolder.game_id] ?? confirmHolder.game_id}).`,
      );
      setConfirmHolder(null);
      await refresh();
    } catch (error) {
      setFormError(describeApiError(error, 'Could not save the featured game.'));
    } finally {
      setSaving(false);
    }
  };

  const handleClear = async (date: string) => {
    try {
      await clearFeaturedGame(date);
      toast.info(`Featured pick for ${date} cleared.`);
      await refresh();
    } catch (error) {
      toast.error(describeApiError(error, 'Could not clear the featured game.'));
    }
  };

  const runs = useMemo(() => runsByDate(featured), [featured]);

  return (
    <Card className="[border-radius:var(--edge-card)] border-border/60 bg-card/70 backdrop-blur">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-xl">
              <Star className="h-5 w-5" />
              Featured game
            </CardTitle>
            <CardDescription>
              One spotlight per UTC date, and one game at most 7 days in a row.
              Featuring highlights a game -- it never creates, expires or
              otherwise touches any session.
            </CardDescription>
          </div>
          <CalendarClock className="h-5 w-5 shrink-0 text-muted-foreground" />
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-2">
            <Label htmlFor="featured-date">Date (UTC)</Label>
            <Input
              id="featured-date"
              type="date"
              value={gameDate}
              onChange={(event) => {
                setGameDate(event.target.value);
                setFormError(null);
              }}
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="featured-game">Game</Label>
            <Select value={gameId} onValueChange={setGameId}>
              <SelectTrigger id="featured-game" className="w-56">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {FEATURED_IDS.map((id) => (
                  <SelectItem key={id} value={id}>
                    {GAME_LABELS[id]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button type="button" onClick={handleSave} disabled={!gameDate || saving}>
            {saving ? 'Saving...' : 'Set featured'}
          </Button>
        </div>
        {formError && (
          <p role="alert" className="text-sm font-medium text-destructive">
            {formError}
          </p>
        )}
        <AlertDialog
          open={confirmHolder !== null}
          onOpenChange={(open) => {
            if (!open) setConfirmHolder(null);
          }}
        >
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Erase this featured pick?</AlertDialogTitle>
              <AlertDialogDescription>
                {confirmHolder !== null && (
                  <>
                    {GAME_LABELS[confirmHolder.game_id] ?? confirmHolder.game_id} is
                    currently featured for {confirmHolder.game_date}. Setting{' '}
                    {GAME_LABELS[gameId] ?? gameId} as featured will erase that pick
                    for {confirmHolder.game_date} and set {GAME_LABELS[gameId] ?? gameId}{' '}
                    as the new featured game. Scores already earned keep their 2x and
                    are never recalculated.
                  </>
                )}
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel disabled={saving}>Cancel</AlertDialogCancel>
              <AlertDialogAction onClick={() => void handleConfirmReplace()} disabled={saving}>
                {saving ? 'Replacing…' : 'Erase and feature'}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
        <div className="flex flex-col gap-2 border-t border-border/40 pt-4">
          <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Scheduled spotlight
          </h4>
          {loading && <p className="text-sm text-muted-foreground">Loading...</p>}
          {listError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {listError}
            </p>
          )}
          {!loading && !listError && featured.length === 0 && (
            <p className="text-sm text-muted-foreground">
              Nothing featured yet. Pick a date above and it appears here.
            </p>
          )}
          {featured.length > 0 && (
            <ul className="flex flex-col gap-2">
              {featured.map((entry) => {
                const run = runs.get(entry.game_date);
                return (
                <li
                  key={entry.id}
                  className="flex items-center justify-between gap-3 [border-radius:var(--edge-card-sm)] border border-border/60 p-3"
                >
                  <span className="flex min-w-0 flex-wrap items-center gap-2 text-sm">
                    <span className="font-mono font-medium">{entry.game_date}</span>
                    {' — '}
                    {GAME_LABELS[entry.game_id] ?? entry.game_id}
                    {entry.game_date === todayIso() && (
                      <Badge>Today</Badge>
                    )}
                    {run !== undefined && run.run > 1 && (
                      <Badge variant="outline">
                        Day {run.pos} of {run.run}
                      </Badge>
                    )}
                    <span className="text-xs text-muted-foreground">
                      by {entry.created_by}
                    </span>
                  </span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => void handleClear(entry.game_date)}
                    aria-label={`Clear the featured game for ${entry.game_date}`}
                  >
                    <X />
                  </Button>
                </li>
                );
              })}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
};

export default AdminFeaturedSchedule;
