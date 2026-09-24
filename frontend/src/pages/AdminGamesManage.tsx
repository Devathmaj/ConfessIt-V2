import { AdminAnagramConfig } from '@/components/AdminAnagramConfig';
import { AdminCoupleConfig } from '@/components/AdminCoupleConfig';
import { AdminFeaturedSchedule } from '@/components/AdminFeaturedSchedule';
import { AdminGameLifecycleConfig } from '@/components/AdminGameLifecycleConfig';
import { AdminWordleConfig } from '@/components/AdminWordleConfig';
import { useAuth } from '@/contexts/AuthContext';
import { CAPABILITIES } from '@/lib/capabilities';

/**
 * The games console: the spotlight schedule, per-game status and reset
 * cadences, and the Wordle, Anagram and Couple Questionnaire sessions.
 *
 * Everything here goes to the server. The featured schedule pins at most one
 * game per UTC date without touching any session; the lifecycle panel owns
 * availability and each game's own reset cadence; the per-game cards below
 * Update sessions (terminate-then-create) and schedule future manual words.
 * Screens render only for an account holding `games:manage`, and the server
 * refuses the same writes again regardless of what this gate believes.
 */
export const AdminGamesManage = () => {
  const { can } = useAuth();
  const mayManage = can(CAPABILITIES.GAMES_MANAGE);

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold">Manage games</h1>
        <p className="text-sm text-muted-foreground">
          The featured spotlight, per-game status and session resets, and the current
          Wordle, Anagram and Couple Questionnaire sessions. Changes here show up on the
          player-facing section straight away.
        </p>
      </header>

      {mayManage && (
        <div className="grid gap-6 lg:grid-cols-2">
          <AdminFeaturedSchedule />
          <AdminGameLifecycleConfig />
        </div>
      )}

      {mayManage && <AdminWordleConfig />}
      {mayManage && <AdminAnagramConfig />}
      {mayManage && <AdminCoupleConfig />}
    </div>
  );
};

export default AdminGamesManage;
