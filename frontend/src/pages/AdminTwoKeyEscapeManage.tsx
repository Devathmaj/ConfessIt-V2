import { NavLink } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { AdminTwoKeyEscapeConfig } from '@/components/AdminTwoKeyEscapeConfig';

/**
 * The Two-Key Escape console: its own page under /admin/games/manage,
 * mirroring the Connections and Trivia Drop editors.
 *
 * All Two-Key Escape configuration lives here -- availability, reset cadence
 * and the maze edge length. The manage overview only links here with title,
 * short description and an Open Editor option.
 */
export const AdminTwoKeyEscapeManage = () => {
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
        <h1 className="text-3xl font-semibold">Two-Key Escape</h1>
        <p className="text-sm text-muted-foreground">
          One maze, two roles — navigate together, asynchronously. Sessions
          roll on this game&apos;s own reset cadence with a fresh maze each
          period at the stored size.
        </p>
      </header>

      <AdminTwoKeyEscapeConfig />
    </div>
  );
};

export default AdminTwoKeyEscapeManage;
