import { useCallback, useEffect, useState } from 'react';
import { Star } from 'lucide-react';
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
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { describeApiError } from '@/lib/apiError';
import { getGameById } from '@/lib/gamesCatalog';
import { getFeaturedGames, replaceFeaturedGame } from '@/services/api';

/** Today as UTC `YYYY-MM-DD`, the only calendar the spotlight speaks. */
const todayIso = () => new Date().toISOString().slice(0, 10);

const gameLabel = (gameId: string): string =>
  getGameById(gameId)?.name ?? gameId;

/**
 * The per-game spotlight action: one big "Set as Featured" for today.
 *
 * Game pages own no scheduling of the spotlight -- the schedule lives on the
 * manage overview. This card only pins *this* game for *today*. When another
 * game holds today, the admin confirms a replacement first: the dialog names
 * the displaced game and states the scoring rule, so the swap is never a
 * surprise.
 *
 * Scoring is snapshot-based: finalization records `is_featured` once, so
 * points already earned keep their 2x and are never recalculated. Replacing
 * the spotlight only changes which game earns 2x from now on.
 */
export const AdminSetFeaturedCard = ({
  gameId,
  gameName,
}: {
  gameId: string;
  gameName: string;
}) => {
  const { logActivity } = useGamesConfig();

  const [featuredId, setFeaturedId] = useState<string | null>(null);
  const [serverDate, setServerDate] = useState<string>(todayIso);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      // The admin spotlight list, not the player-facing today endpoint: the
      // player route refuses staff accounts, while this list is gated on the
      // same capability as the rest of this console.
      const all = await getFeaturedGames();
      const today = todayIso();
      const match = all.find((entry) => entry.game_date === today) ?? null;
      setFeaturedId(match?.game_id ?? null);
      setServerDate(match?.game_date ?? today);
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

  const isFeatured = featuredId === gameId;
  const otherLabel = featuredId && !isFeatured ? gameLabel(featuredId) : null;

  const handleConfirm = async () => {
    setSaving(true);
    setError(null);
    try {
      const created = await replaceFeaturedGame(serverDate, gameId);
      setFeaturedId(created.game_id);
      setServerDate(created.game_date);
      toast.success(`${gameName} is featured for ${created.game_date}.`);
      logActivity({
        kind: 'featured',
        gameId,
        message: `${gameName} set as today's featured game`,
      });
      setConfirmOpen(false);
    } catch (saveError) {
      setError(describeApiError(saveError, 'Could not set the featured game.'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Card className="rounded-2xl border-amber-500/40 bg-amber-500/5 backdrop-blur">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <CardTitle className="flex items-center gap-2 text-xl">
                <Star className="h-5 w-5 text-amber-500" />
                Featured spotlight
              </CardTitle>
              <CardDescription>
                The featured game earns 2x score. Scores already earned keep
                their multiplier and are never recalculated — switching only
                changes which game earns 2x from now on.
              </CardDescription>
            </div>
            {isFeatured && <Badge>Featured today</Badge>}
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {loading ? (
            <p className="text-sm text-muted-foreground">Checking today&apos;s spotlight…</p>
          ) : isFeatured ? (
            <p className="text-sm">
              <span className="font-semibold">{gameName}</span> holds today&apos;s
              spotlight ({serverDate}). Plays on this game earn 2x until the
              spotlight moves.
            </p>
          ) : otherLabel ? (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="text-sm">
                <span className="font-semibold">{otherLabel}</span> is featured
                today. Setting <span className="font-semibold">{gameName}</span> as
                featured will remove {otherLabel} from the spotlight.
              </p>
              <Button type="button" size="lg" onClick={() => setConfirmOpen(true)}>
                <Star />
                Set as Featured
              </Button>
            </div>
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="text-sm text-muted-foreground">
                No game holds today&apos;s spotlight yet.
              </p>
              <Button type="button" size="lg" onClick={() => void handleConfirm()} disabled={saving}>
                <Star />
                {saving ? 'Featuring…' : 'Set as Featured'}
              </Button>
            </div>
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
              {otherLabel} is currently featured for {serverDate}. Setting {gameName} as
              featured will remove {otherLabel} from the spotlight and set {gameName} as
              the new featured game. Scores already earned keep their 2x and are never
              recalculated — from now on {gameName} earns 2x instead of {otherLabel}.
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

export default AdminSetFeaturedCard;
