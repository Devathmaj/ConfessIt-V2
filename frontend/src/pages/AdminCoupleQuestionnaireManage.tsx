import { NavLink } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { AdminCoupleConfig } from '@/components/AdminCoupleConfig';

/**
 * The Couple Questionnaire console: its own page under /admin/games/manage,
 * mirroring the Connections and Trivia Drop editors.
 *
 * All Couple Questionnaire configuration lives here -- availability, reset
 * cadence, the featured day and its history. The manage overview only links
 * here with title, short description and an Open Editor option.
 */
export const AdminCoupleQuestionnaireManage = () => {
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
        <h1 className="text-3xl font-semibold">Couple Questionnaire</h1>
        <p className="text-sm text-muted-foreground">
          Answer for yourself, then predict your partner — 15 and 15. One
          featured day at a time; the questionnaire rolls on its own cadence.
        </p>
      </header>

      <AdminCoupleConfig />
    </div>
  );
};

export default AdminCoupleQuestionnaireManage;
