import { useCallback, useEffect, useState } from 'react';
import { HeartHandshake } from 'lucide-react';

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { AdminGameStatusControls } from '@/components/AdminGameStatusControls';
import { describeApiError } from '@/lib/apiError';
import { getCurrentAdminCoupleSession } from '@/services/api';

/**
 * The Couple Questionnaire's gameplay configuration.
 *
 * The mechanics are fixed by the application: the 50-question bank is shared
 * content seeded at startup, and every couple gets a fresh set of 25
 * questions each period. There is nothing per-period for an administrator to
 * type, draw or schedule, and no shared session history to browse -- sessions
 * are per-couple, not one puzzle the whole platform shares. The spotlight is
 * owned by the page's Featured card, not by this section.
 */
export const AdminCoupleConfig = () => {
  const [session, setSession] = useState<{
    id: string;
    period_start_date: string | null;
    period_end_date: string | null;
  } | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setSession(await getCurrentAdminCoupleSession());
      setSessionError(null);
    } catch (error) {
      setSession(null);
      setSessionError(describeApiError(error, 'Could not load the current session.'));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-xl">
              <HeartHandshake className="h-5 w-5" />
              Couple Questionnaire
            </CardTitle>
            <CardDescription>
              Sessions roll on this game&apos;s own reset cadence with a fresh
              question set per couple. There is no per-period content to author
              and no shared session history for this game.
            </CardDescription>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        <AdminGameStatusControls gameId="couple-questionnaire" gameName="Couple Questionnaire" />

        <div className="flex flex-col gap-3 border-t border-border/40 pt-4">
          <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Current session
          </h4>
          {sessionError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {sessionError}
            </p>
          )}
          {!sessionError && !session && (
            <p className="text-sm text-muted-foreground">
              No active session right now. The reset loop opens the next one automatically.
            </p>
          )}
          {session && (
            <p className="rounded-xl border border-border/60 bg-muted/40 p-3 text-sm">
              Current session period:{' '}
              <span className="font-mono font-medium">
                {session.period_start_date ?? '—'}
                {session.period_end_date ? ` → ${session.period_end_date}` : ''}
              </span>{' '}
              <span className="text-muted-foreground">
                — every couple gets a fresh set of 25 questions each period.
              </span>
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
};

export default AdminCoupleConfig;
