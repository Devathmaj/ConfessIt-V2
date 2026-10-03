import { HeartHandshake } from 'lucide-react';

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { AdminGameStatusControls } from '@/components/AdminGameStatusControls';

/**
 * The Plot Twist gameplay configuration (barebones).
 *
 * Mirrors the Reaction Time console: availability plus the reset cadence,
 * and nothing else. There is no per-period content to author and no shared
 * session history yet — gameplay lands later.
 */
export const AdminPlotTwistConfig = () => {
  return (
    <Card className="[border-radius:var(--edge-card)] border-border/60 bg-card/70 backdrop-blur">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-xl">
              <HeartHandshake className="h-5 w-5" />
              Plot Twist
            </CardTitle>
            <CardDescription>
              A couple game with no playable sessions yet. Availability and the
              reset cadence are the only knobs — there is no per-period content
              to author and no session history for this game.
            </CardDescription>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        <AdminGameStatusControls gameId="plot-twist" gameName="Plot Twist" />

        <div className="flex flex-col gap-3 border-t border-border/40 pt-4">
          <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Current session
          </h4>
          <p className="text-sm text-muted-foreground">
            No active session right now. Sessions open automatically once gameplay
            lands; until then this game stays listed as coming soon.
          </p>
        </div>
      </CardContent>
    </Card>
  );
};

export default AdminPlotTwistConfig;
