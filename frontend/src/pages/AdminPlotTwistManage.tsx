import { NavLink } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { AdminSetFeaturedCard } from '@/components/AdminSetFeaturedCard';
import { AdminPlotTwistConfig } from '@/components/AdminPlotTwistConfig';

/**
 * The Plot Twist console: its own page under /admin/games/manage,
 * mirroring the Reaction Time editor.
 *
 * All Plot Twist configuration lives here -- availability and the reset
 * cadence. The manage overview only links here with title, short description
 * and an Open Editor option.
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
          A couple game — gameplay lands soon. Availability rolls through the
          same hidden/disabled switches as every other title.
        </p>
      </header>

      <AdminSetFeaturedCard gameId="plot-twist" gameName="Plot Twist" />
      <AdminPlotTwistConfig />
    </div>
  );
};

export default AdminPlotTwistManage;
