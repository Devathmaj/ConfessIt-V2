import { useState } from 'react';
import { NavLink } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { AdminFeaturedRangeCard } from '@/components/AdminFeaturedRangeCard';
import { AdminFeaturedSchedule } from '@/components/AdminFeaturedSchedule';

/**
 * The dedicated featured schedule: every date the spotlight is pinned on,
 * in depth, plus the tools to plan it.
 *
 * The manage overview keeps only today's front door (one game, set for
 * today); everything else lives here -- pinning single dates, featuring a
 * whole span, and clearing days. One game per UTC date, and one game at most
 * 7 days in a row: a span that would run longer is refused, but week blocks
 * separated by another game are fine. Featuring never creates or touches any
 * session, and league scores already earned keep their 2x -- switching only
 * changes which game earns 2x from now on.
 */
export const AdminFeaturedManage = () => {
  const [scheduleKey, setScheduleKey] = useState(0);

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
        <h1 className="text-3xl font-semibold">Featured schedule</h1>
        <p className="text-sm text-muted-foreground">
          Every date the spotlight is pinned on. One game per date, at most 7
          days in a row — scores already earned keep their 2x either way.
        </p>
      </header>

      <AdminFeaturedRangeCard onChanged={() => setScheduleKey((key) => key + 1)} />
      <AdminFeaturedSchedule key={scheduleKey} />
    </div>
  );
};

export default AdminFeaturedManage;
