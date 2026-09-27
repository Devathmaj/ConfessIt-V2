import { NavLink } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { AdminSetFeaturedCard } from '@/components/AdminSetFeaturedCard';
import { AdminReactionTimeConfig } from '@/components/AdminReactionTimeConfig';

/**
 * The Reaction Time console: its own page under /admin/games/manage,
 * mirroring the Connections and Trivia Drop editors.
 *
 * All Reaction Time configuration lives here -- availability, reset cadence
 * and the current session. The manage overview only links here with
 * title, short description and an Open Editor option.
 */
export const AdminReactionTimeManage = () => {
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
        <h1 className="text-3xl font-semibold">Reaction Time</h1>
        <p className="text-sm text-muted-foreground">
          Wait for green, then click as fast as you can — five tries. Play
          rolls on its own cadence with five fresh submissions per period.
        </p>
      </header>

      <AdminSetFeaturedCard gameId="reaction-time" gameName="Reaction Time" />
      <AdminReactionTimeConfig />
    </div>
  );
};

export default AdminReactionTimeManage;
