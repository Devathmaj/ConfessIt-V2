import { NavLink } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { AdminSetFeaturedCard } from '@/components/AdminSetFeaturedCard';
import { AdminPlotTwistConfig } from '@/components/AdminPlotTwistConfig';

/**
 * The Plot Twist console: its own page under /admin/games/manage.
 *
 * Sessions are generated and published automatically on the configured
 * schedule; the console configures difficulty and frequency, inspects the
 * current story, pages session history and revokes the live session. The
 * manage overview only links here with title, short description and an
 * Open Editor option.
 */
export const AdminPlotTwistManage = () => {
  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-2">
        <NavLink
          to="/admin/games/manage"
          className="inline-flex w-fit items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3 w-3" />
          Back to Manage games
        </NavLink>
        <h1 className="text-3xl font-semibold">Plot Twist</h1>
        <p className="text-sm text-muted-foreground">
          A branching story couples write together — generated before play,
          executed deterministically. Sessions publish automatically on the
          configured frequency; configure the difficulty, inspect the current
          story, or revoke it.
        </p>
      </header>

      <AdminSetFeaturedCard gameId="plot-twist" gameName="Plot Twist" />
      <AdminPlotTwistConfig />
    </div>
  );
};

export default AdminPlotTwistManage;
