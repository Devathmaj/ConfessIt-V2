import { NavLink } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { AdminSetFeaturedCard } from '@/components/AdminSetFeaturedCard';
import { AdminAnagramConfig } from '@/components/AdminAnagramConfig';

/**
 * The Anagram console: its own page under /admin/games/manage, mirroring the
 * Connections and Trivia Drop editors.
 *
 * All Anagram configuration lives here -- availability, reset cadence, the
 * current session update, future words and history. The manage overview only
 * links here with title, short description and an Open Editor option.
 */
export const AdminAnagramManage = () => {
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
        <h1 className="text-3xl font-semibold">Anagram</h1>
        <p className="text-sm text-muted-foreground">
          Unscramble the letters in five guesses or fewer. Update ends the
          current session and opens a new one — history keeps its own word.
        </p>
      </header>

      <AdminSetFeaturedCard gameId="anagram" gameName="Anagram" />
      <AdminAnagramConfig />
    </div>
  );
};

export default AdminAnagramManage;
