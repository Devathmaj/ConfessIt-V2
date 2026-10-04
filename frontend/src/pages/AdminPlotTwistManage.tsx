import { NavLink } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { AdminSetFeaturedCard } from '@/components/AdminSetFeaturedCard';
import { AdminPlotTwistConfig } from '@/components/AdminPlotTwistConfig';

/**
 * The Plot Twist console: its own page under /admin/games/manage.
 *
 * Story generation, review, validation and publication all live here --
 * availability, the reset cadence and the current session alongside them.
 * The manage overview only links here with title, short description and an
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
          executed deterministically. Configure the event, generate and review
          stories, then publish a validated version.
        </p>
      </header>

      <AdminSetFeaturedCard gameId="plot-twist" gameName="Plot Twist" />
      <AdminPlotTwistConfig />
    </div>
  );
};

export default AdminPlotTwistManage;
