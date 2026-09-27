import { useState } from 'react';
import { NavLink } from 'react-router-dom';
import { RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { AdminFeaturedSchedule } from '@/components/AdminFeaturedSchedule';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/contexts/AuthContext';
import { CAPABILITIES } from '@/lib/capabilities';
import { AVAILABLE_GAMES } from '@/lib/gamesCatalog';
import { describeApiError } from '@/lib/apiError';
import { runScheduledResets } from '@/services/api';

/**
 * The sub-editor each catalogue title opens. The manage overview never
 * configures a game inline -- it only lists the title, the short description
 * and an Open Editor option that walks to the game's own page.
 */
const MANAGE_ROUTES: Record<string, string> = {
  wordle: '/admin/games/manage/wordle',
  anagram: '/admin/games/manage/anagram',
  'couple-questionnaire': '/admin/games/manage/couple-questionnaire',
  'two-key-escape': '/admin/games/manage/two-key-escape',
  'reaction-time': '/admin/games/manage/reaction-time',
  connections: '/admin/games/manage/connections',
  'trivia-drop': '/admin/games/manage/trivia-drop',
};

/**
 * The games console: the spotlight schedule plus one row per game.
 *
 * Every game's configuration -- availability, reset cadence, sessions,
 * schedules and history -- lives on that game's own editor page, mirroring
 * the Connections and Trivia Drop editors. This screen keeps the featured
 * spotlight (which belongs to no single game) and the manual reset trigger,
 * and otherwise only lists each title with its short description and an Open
 * Editor option.
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
            The featured spotlight and every game&apos;s own editor. Open a
            game below to change its sessions and settings — changes show up
            on the player-facing section straight away.
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

      {mayManage && (
        <div className="flex flex-col gap-3">
          {AVAILABLE_GAMES.map((game) => {
            const Icon = game.icon;
            const to = MANAGE_ROUTES[game.id] ?? `/admin/games/manage/${game.id}`;
            return (
              <NavLink
                key={game.id}
                to={to}
                aria-label={`Open ${game.name} editor`}
                className="flex items-center justify-between gap-3 rounded-2xl border border-border/60 bg-card/70 px-5 py-4 backdrop-blur transition-colors hover:border-romantic/50"
              >
                <span className="flex items-center gap-3">
                  <Icon className="h-5 w-5 shrink-0" />
                  <span className="min-w-0">
                    <span className="block text-base font-semibold">{game.name}</span>
                    <span className="block text-sm text-muted-foreground">
                      {game.tagline}
                    </span>
                  </span>
                </span>
                <span className="shrink-0 text-sm font-semibold text-romantic">Open editor</span>
              </NavLink>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default AdminGamesManage;
