import { useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { AdminAnagramConfig } from '@/components/AdminAnagramConfig';
import { AdminCoupleConfig } from '@/components/AdminCoupleConfig';
import { AdminFeaturedSchedule } from '@/components/AdminFeaturedSchedule';
import { AdminReactionTimeConfig } from '@/components/AdminReactionTimeConfig';
import { AdminWordleConfig } from '@/components/AdminWordleConfig';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/contexts/AuthContext';
import { CAPABILITIES } from '@/lib/capabilities';
import { describeApiError } from '@/lib/apiError';
import { runScheduledResets } from '@/services/api';

/**
 * The games console: the spotlight schedule and the Wordle, Anagram, Couple
 * Questionnaire and Reaction Time cards.
 *
 * Everything here goes to the server. The featured schedule pins at most one
 * game per UTC date without touching any session; each game card owns its own
 * availability and reset cadence alongside its sessions, so there is no
 * separate status section to keep in sync. The Wordle and Anagram cards Update
 * sessions (terminate-then-create) and schedule future manual words, while the
 * Couple and Reaction Time cards hold a featured-day picker plus the live
 * session period because their mechanics are fixed by the app.
 * Screens render only for an account holding `games:manage`, and the server
 * refuses the same writes again regardless of what this gate believes.
 */
export const AdminGamesManage = () => {
  const { can } = useAuth();
  const mayManage = can(CAPABILITIES.GAMES_MANAGE);
  const [resetting, setResetting] = useState(false);

  const handleRunResets = async () => {
    setResetting(true);
    try {
      await runScheduledResets();
      toast.success('Session boundaries evaluated.');
    } catch (error) {
      toast.error(describeApiError(error, 'Could not run the session resets.'));
    } finally {
      setResetting(false);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-2">
          <h1 className="text-3xl font-semibold">Manage games</h1>
          <p className="text-sm text-muted-foreground">
            The featured spotlight and the current Wordle, Anagram, Couple
            Questionnaire and Reaction Time sessions. Changes here show up on the
            player-facing section straight away.
          </p>
        </div>
        {mayManage && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handleRunResets}
            disabled={resetting}
          >
            <RefreshCw />
            {resetting ? 'Evaluating...' : 'Run resets now'}
          </Button>
        )}
      </header>

      {mayManage && <AdminFeaturedSchedule />}

      {mayManage && <AdminWordleConfig />}
      {mayManage && <AdminAnagramConfig />}
      {mayManage && <AdminCoupleConfig />}
      {mayManage && <AdminReactionTimeConfig />}
    </div>
  );
};

export default AdminGamesManage;
