import { useCallback, useEffect, useState } from 'react';
import { CalendarRange } from 'lucide-react';
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
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { describeApiError } from '@/lib/apiError';
import { AVAILABLE_GAMES } from '@/lib/gamesCatalog';
import {
  getFeaturedGames,
  replaceFeaturedRange,
  setFeaturedRange,
  type FeaturedGame,
} from '@/services/api';

/** Today as UTC `YYYY-MM-DD`, the only calendar the schedule speaks. */
const todayIso = () => new Date().toISOString().slice(0, 10);

/** Whole days in `[start, end]`, assuming valid `YYYY-MM-DD` input. */
const spanLength = (start: string, end: string): number =>
  Math.round(
    (new Date(`${end}T00:00:00Z`).getTime() - new Date(`${start}T00:00:00Z`).getTime()) /
      86_400_000,
  ) + 1;

const gameNameFor = (gameId: string): string =>
  AVAILABLE_GAMES.find((game) => game.id === gameId)?.name ?? gameId;

/**
 * Featuring one game for a whole span, start to end inclusive.
 *
 * Any setup may erase what it covers: when the span holds other games'
 * picks, the admin confirms against every listed date first and the
 * replacement lands in one transaction. A span whose consecutive run --
 * merged with the same game's bordering picks -- would pass a week is
 * refused instead. Featuring never creates or touches any session.
 */
export const AdminFeaturedRangeCard = ({ onChanged }: { onChanged: () => void }) => {
  const { logActivity } = useGamesConfig();

  const [gameId, setGameId] = useState<string>(AVAILABLE_GAMES[0]?.id ?? 'wordle');
  const [startDate, setStartDate] = useState<string>(todayIso);
  const [endDate, setEndDate] = useState<string>(todayIso);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [existing, setExisting] = useState<FeaturedGame[]>([]);
  const [confirmConflicts, setConfirmConflicts] = useState<FeaturedGame[]>([]);

  const gameName = gameNameFor(gameId);

  const refreshExisting = useCallback(async () => {
    try {
      setExisting(await getFeaturedGames());
    } catch {
      // Conflict detection degrades to the server's answer: a taken date
      // still comes back as a 409 naming it.
      setExisting([]);
    }
  }, []);

  useEffect(() => {
    void refreshExisting();
  }, [refreshExisting]);

  /** Picks inside `[start, end]` held by other games, oldest date first. */
  const conflictsIn = (start: string, end: string, picked: string): FeaturedGame[] =>
    existing
      .filter(
        (entry) =>
          entry.game_date >= start && entry.game_date <= end && entry.game_id !== picked,
      )
      .sort((a, b) => (a.game_date < b.game_date ? -1 : 1));

  const announce = (created: FeaturedGame[]) => {
    toast.success(
      created.length === 1
        ? `${gameName} featured for ${created[0].game_date}.`
        : `${gameName} featured ${created[0].game_date} to ${created[created.length - 1].game_date} (${created.length} days).`,
    );
    logActivity({
      kind: 'featured',
      gameId,
      message:
        created.length === 1
          ? `${gameName} featured for ${created[0].game_date}`
          : `${gameName} featured ${created[0].game_date} to ${created[created.length - 1].game_date}`,
    });
  };

  const handleSave = async () => {
    if (!startDate || !endDate) return;
    if (startDate > endDate) {
      setFormError('The range start must not be after the range end.');
      return;
    }
    if (spanLength(startDate, endDate) > 7) {
      setFormError(
        'One game may hold the spotlight at most 7 days in a row — shorten the span or feature another game in between.',
      );
      return;
    }
    const conflicts = conflictsIn(startDate, endDate, gameId);
    if (conflicts.length > 0) {
      // Other games' picks would be erased: confirm against every date first.
      setConfirmConflicts(conflicts);
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      announce(await setFeaturedRange(startDate, endDate, gameId));
      await refreshExisting();
      onChanged();
    } catch (error) {
      setFormError(describeApiError(error, 'Could not save the featured span.'));
    } finally {
      setSaving(false);
    }
  };

  const handleConfirmReplace = async () => {
    setSaving(true);
    setFormError(null);
    try {
      const created = await replaceFeaturedRange(startDate, endDate, gameId);
      toast.success(
        created.length === 1
          ? `${gameName} featured for ${created[0].game_date} (replacing ${confirmConflicts.length} ${confirmConflicts.length === 1 ? 'pick' : 'picks'}).`
          : `${gameName} featured ${created[0].game_date} to ${created[created.length - 1].game_date} (${created.length} days, replacing ${confirmConflicts.length} ${confirmConflicts.length === 1 ? 'pick' : 'picks'}).`,
      );
      logActivity({
        kind: 'featured',
        gameId,
        message: `${gameName} featured ${startDate} to ${endDate} (replacing ${confirmConflicts.length})`,
      });
      setConfirmConflicts([]);
      await refreshExisting();
      onChanged();
    } catch (error) {
      setFormError(describeApiError(error, 'Could not save the featured span.'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <CardTitle className="flex items-center gap-2 text-xl">
                <CalendarRange className="h-5 w-5" />
                Feature a span
              </CardTitle>
              <CardDescription>
                One game from a start date to an end date. Picks the span covers
                are listed for confirmation before anything is erased, and the
                run — including this game&apos;s picks bordering the span — may
                not pass 7 days in a row.
              </CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-2">
              <Label htmlFor="featured-range-game">Game</Label>
              <Select
                value={gameId}
                onValueChange={(next) => {
                  setGameId(next);
                  setFormError(null);
                }}
              >
                <SelectTrigger id="featured-range-game" className="w-56">
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
            <div className="flex flex-col gap-2">
              <Label htmlFor="featured-range-start">Start (UTC)</Label>
              <Input
                id="featured-range-start"
                type="date"
                value={startDate}
                onChange={(event) => {
                  setStartDate(event.target.value);
                  setFormError(null);
                }}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="featured-range-end">End (UTC)</Label>
              <Input
                id="featured-range-end"
                type="date"
                value={endDate}
                onChange={(event) => {
                  setEndDate(event.target.value);
                  setFormError(null);
                }}
              />
            </div>
            <Button type="button" onClick={handleSave} disabled={!startDate || !endDate || saving}>
              {saving ? 'Saving...' : 'Feature span'}
            </Button>
          </div>
          {formError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {formError}
            </p>
          )}
        </CardContent>
      </Card>

      <AlertDialog
        open={confirmConflicts.length > 0}
        onOpenChange={(open) => {
          if (!open) setConfirmConflicts([]);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Erase these featured picks?</AlertDialogTitle>
            <AlertDialogDescription>
              Setting {gameName} from {startDate} to {endDate} will erase{' '}
              {confirmConflicts.length}{' '}
              {confirmConflicts.length === 1 ? 'pick' : 'picks'}:
            </AlertDialogDescription>
          </AlertDialogHeader>
          <ul className="flex max-h-48 flex-col gap-1 overflow-y-auto rounded-xl border border-border/60 p-3 text-sm">
            {confirmConflicts.map((entry) => (
              <li key={entry.game_date} className="flex items-center gap-2">
                <span className="font-mono font-medium">{entry.game_date}</span>
                <span className="text-muted-foreground">
                  {gameNameFor(entry.game_id)}
                </span>
              </li>
            ))}
          </ul>
          <p className="text-xs text-muted-foreground">
            Scores already earned keep their 2x and are never recalculated.
          </p>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={saving}>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => void handleConfirmReplace()} disabled={saving}>
              {saving ? 'Replacing…' : 'Erase and feature'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
};

export default AdminFeaturedRangeCard;
