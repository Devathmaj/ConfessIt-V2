import { useCallback, useEffect, useState } from 'react';
import { CalendarClock, Star, X } from 'lucide-react';
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
] as const;

const GAME_LABELS: Record<string, string> = {
  wordle: 'Wordle',
  anagram: 'Anagram',
  'couple-questionnaire': 'Couple Questionnaire',
  'reaction-time': 'Reaction Time',
  connections: 'Connections',
  'trivia-drop': 'Trivia Drop',
  'two-key-escape': 'Two-Key Escape',
};

/** Today as UTC `YYYY-MM-DD`, the only calendar the schedule speaks. */
const todayIso = () => new Date().toISOString().slice(0, 10);

/**
 * The spotlight schedule, owned by the server.
 *
 * One game per UTC date, enforced by the database as well as this form: two
 * games on one day race a unique constraint and one of them is refused. The
 * same game may hold many dates. Featuring never creates or touches a session
 * -- a non-featured game keeps its own sessions and stays playable.
 */
export const AdminFeaturedSchedule = () => {
  const [featured, setFeatured] = useState<FeaturedGame[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [gameDate, setGameDate] = useState<string>(todayIso);
  const [gameId, setGameId] = useState<string>('wordle');
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

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

  const handleClear = async (date: string) => {
    try {
      await clearFeaturedGame(date);
      toast.info(`Featured pick for ${date} cleared.`);
      await refresh();
    } catch (error) {
      toast.error(describeApiError(error, 'Could not clear the featured game.'));
    }
  };

  return (
    <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-xl">
              <Star className="h-5 w-5" />
              Featured game
            </CardTitle>
            <CardDescription>
              One spotlight per UTC date. Featuring highlights a game -- it never creates,
              expires or otherwise touches any session.
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
              {featured.map((entry) => (
                <li
                  key={entry.id}
                  className="flex items-center justify-between gap-3 rounded-xl border border-border/60 p-3"
                >
                  <span className="min-w-0 text-sm">
                    <span className="font-mono font-semibold">{entry.game_date}</span>
                    {' — '}
                    {GAME_LABELS[entry.game_id] ?? entry.game_id}
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
              ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
};

export default AdminFeaturedSchedule;
