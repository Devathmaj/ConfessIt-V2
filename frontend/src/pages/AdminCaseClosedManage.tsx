import { NavLink } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { AdminSetFeaturedCard } from '@/components/AdminSetFeaturedCard';
import { AdminCaseClosedConfig } from '@/components/AdminCaseClosedConfig';

/**
 * The Case Closed console: its own page under /admin/games/manage,
 * mirroring the Reaction Time editor.
 *
 * Cases are generated and published automatically on the configured
 * schedule; the console configures difficulty and frequency, inspects the
 * current case, pages session history and revokes the live event. The manage
 * overview only links here with title, short description and an Open Editor
 * option.
 */
export const AdminCaseClosedManage = () => {
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
        <h1 className="text-3xl font-semibold">Case Closed</h1>
        <p className="text-sm text-muted-foreground">
          A cooperative mystery for couples. New cases publish automatically on
          the configured frequency; configure the difficulty, inspect the
          current case, or revoke it.
        </p>
      </header>

      <AdminSetFeaturedCard gameId="case-closed" gameName="Case Closed" />
      <AdminCaseClosedConfig />
    </div>
  );
};

export default AdminCaseClosedManage;
