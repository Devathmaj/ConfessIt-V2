import { useCallback, useEffect, useState } from 'react';
import { ChevronDown, ChevronUp, Dices, Shuffle, X } from 'lucide-react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { AdminGameStatusControls } from '@/components/AdminGameStatusControls';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { describeApiError } from '@/lib/apiError';
import {
  createAnagramGame,
  deleteScheduledAnagramWord,
  drawAnagramAnswer,
  getAdminAnagramGame,
  getAdminAnagramGames,
  getCurrentAdminAnagramSession,
  getGameConfigs,
  getScheduledAnagramWords,
  scheduleAnagramWord,
  updateGameConfig,
  type AnagramDraw,
  type AnagramGame,
  type AnagramGamePayload,
  type ScheduledWord,
} from '@/services/api';

/** Today as UTC `YYYY-MM-DD`, the only calendar the schedule speaks. */
const todayIso = () => new Date().toISOString().slice(0, 10);

type Mode = AnagramGamePayload['mode'];
type ContentMode = 'random' | 'manual';

/** Session-mode provenance to content-mode behaviour: a typed word is manual. */
const toContentMode = (form: Mode): ContentMode => (form === 'random' ? 'random' : 'manual');
/** Content-mode behaviour to the form it renders. */
const toFormMode = (content: ContentMode | null): Mode =>
  content === 'random' ? 'random' : 'custom';

/**
 * Updating the current Anagram session.
 *
 * Two ways in, both ending in the same Update: type a word, or let the server
 * draw one. "Update" ends the current session and opens a brand-new one -- the
 * old row stays as history with its player progress, the new session starts
 * from scratch for today under the game's own reset cadence. Future words are
 * scheduled below, inside the finite horizon, and wait until their period opens.
 */
export const AdminAnagramConfig = () => {
  const { logActivity } = useGamesConfig();

  const [games, setGames] = useState<AnagramGame[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [current, setCurrent] = useState<AnagramGame | null>(null);

  // The content mode the server runs on, and a staged change to it. Clicking
  // "Type a word" / "Draw for me" stages manual / random but saves nothing --
  // the staged value needs an explicit Save, so clicking back and forth never
  // flips production config by itself. The form below follows the staged value
  // so the pending mode can be tried immediately.
  const [serverMode, setServerMode] = useState<ContentMode | null>(null);
  const [stagedMode, setStagedMode] = useState<ContentMode | null>(null);
  const [savingMode, setSavingMode] = useState(false);
  const [modeError, setModeError] = useState<string | null>(null);
  const formMode = toFormMode(stagedMode ?? serverMode);
  const hasUnsavedMode = stagedMode !== null && stagedMode !== serverMode;

  const [word, setWord] = useState('');
  const [drawn, setDrawn] = useState<AnagramDraw | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [scheduleDate, setScheduleDate] = useState<string>(todayIso);
  const [scheduleWord, setScheduleWord] = useState('');
  const [scheduled, setScheduled] = useState<ScheduledWord[]>([]);
  const [scheduling, setScheduling] = useState(false);
  const [scheduleError, setScheduleError] = useState<string | null>(null);

  const [detail, setDetail] = useState<AnagramGame | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  // Session history stays collapsed so a long season of past sessions does not
  // push the working controls off the screen.
  const [historyOpen, setHistoryOpen] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setGames(await getAdminAnagramGames());
      setListError(null);
    } catch (error) {
      setListError(describeApiError(error, 'Could not load the configured puzzles.'));
    } finally {
      setLoading(false);
    }
    try {
      setCurrent(await getCurrentAdminAnagramSession());
    } catch {
      setCurrent(null);
    }
    try {
      setScheduled(await getScheduledAnagramWords());
      setScheduleError(null);
    } catch (error) {
      setScheduleError(describeApiError(error, 'Could not load the scheduled words.'));
    }
    try {
      const configs = await getGameConfigs();
      setServerMode(
        configs.find((entry) => entry.game_id === 'anagram')?.content_mode ?? null,
      );
      setStagedMode(null);
      setModeError(null);
    } catch {
      // Keep the last known mode; the form still works, only the staged save waits.
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  /** Stage a content mode without saving it; staging the saved value clears. */
  const selectMode = (form: Mode) => {
    const next = toContentMode(form);
    setStagedMode(next === serverMode ? null : next);
    setModeError(null);
    setFormError(null);
    if (form === 'custom') {
      setDrawn(null);
    } else {
      setWord('');
    }
  };

  const handleSaveMode = async () => {
    if (stagedMode === null) return;
    setSavingMode(true);
    setModeError(null);
    try {
      await updateGameConfig('anagram', { content_mode: stagedMode });
      toast.success(
        stagedMode === 'random'
          ? 'Anagram will run on random words from the next session onwards.'
          : 'Anagram will wait for a manual word from the next session onwards.',
      );
      logActivity({
        kind: 'settings',
        gameId: 'anagram',
        message: `Anagram content mode set to ${stagedMode} (next session onwards)`,
      });
      await refresh();
    } catch (error) {
      setModeError(describeApiError(error, 'Could not save the content mode.'));
    } finally {
      setSavingMode(false);
    }
  };

  const handleDraw = async () => {
    setDrawing(true);
    setFormError(null);
    try {
      setDrawn(await drawAnagramAnswer(todayIso()));
    } catch (error) {
      setFormError(describeApiError(error, 'Could not draw a word.'));
    } finally {
      setDrawing(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setFormError(null);
    try {
      const payload: AnagramGamePayload =
        formMode === 'custom'
          ? { mode: 'custom', word: word.trim() }
          : { mode: 'random', word: drawn?.word, token: drawn?.token };
      const created = await createAnagramGame(payload);

      toast.success(`Anagram session updated for ${created.game_date}.`);
      logActivity({
        kind: 'puzzle',
        gameId: 'anagram',
        message: `Anagram session updated for ${created.game_date}`,
      });
      setWord('');
      setDrawn(null);
      setDetail(null);
      setDetailError(null);
      await refresh();
    } catch (error) {
      setFormError(describeApiError(error, 'Could not update the session.'));
    } finally {
      setSaving(false);
    }
  };

  const handleSchedule = async () => {
    if (!scheduleDate || !scheduleWord.trim()) return;
    setScheduling(true);
    setScheduleError(null);
    try {
      const created = await scheduleAnagramWord(scheduleDate, scheduleWord.trim());
      toast.success(`Anagram word scheduled for ${created.target_date}.`);
      setScheduleWord('');
      await refresh();
    } catch (error) {
      setScheduleError(describeApiError(error, 'Could not schedule the word.'));
    } finally {
      setScheduling(false);
    }
  };

  const handleDeleteScheduled = async (targetDate: string) => {
    try {
      await deleteScheduledAnagramWord(targetDate);
      toast.info(`Scheduled word for ${targetDate} withdrawn.`);
      await refresh();
    } catch (error) {
      setScheduleError(describeApiError(error, 'Could not withdraw the word.'));
    }
  };

  const openDetail = async (gameId: string) => {
    setDetail(null);
    setDetailError(null);
    try {
      setDetail(await getAdminAnagramGame(gameId));
    } catch (error) {
      setDetailError(describeApiError(error, 'Could not load that puzzle.'));
    }
  };

  const readyToSave = formMode === 'custom' ? word.trim().length > 0 : drawn !== null;

  return (
    <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-xl">
              <Shuffle className="h-5 w-5" />
              Anagram
            </CardTitle>
            <CardDescription>
              Update ends the current session and opens a new one. History and player
              progress stay attached to the old session. Switching between typing and
              drawing stages the content mode for the next session onwards -- save it
              to apply.
            </CardDescription>
          </div>
          {current && (
            <Badge variant={current.status === 'pending' ? 'destructive' : 'default'}>
              {current.status === 'pending' ? 'Pending — word not set' : 'Active'}
            </Badge>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        <AdminGameStatusControls gameId="anagram" gameName="Anagram" />

        {current && (
          <p className="rounded-xl border border-border/60 bg-muted/40 p-3 text-sm">
            Current session:{' '}
            <span className="font-mono font-semibold uppercase">
              {current.answer ?? 'no word yet'}
            </span>{' '}
            <span className="text-muted-foreground">
              ({current.game_date}
              {current.period_end_date ? ` → ${current.period_end_date}` : ''})
            </span>
          </p>
        )}

        <div className="flex flex-col gap-4">
          <div className="flex gap-2" role="group" aria-label="Where the word comes from">
            <Button
              type="button"
              variant={formMode === 'custom' ? 'default' : 'outline'}
              size="sm"
              aria-pressed={formMode === 'custom'}
              onClick={() => selectMode('custom')}
            >
              Type a word
            </Button>
            <Button
              type="button"
              variant={formMode === 'random' ? 'default' : 'outline'}
              size="sm"
              aria-pressed={formMode === 'random'}
              onClick={() => selectMode('random')}
            >
              <Dices />
              Draw for me
            </Button>
          </div>

          {hasUnsavedMode && stagedMode !== null && (
            <div
              role="status"
              className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-500/50 bg-amber-500/10 p-3"
            >
              <p className="text-sm">
                <span className="font-semibold">Unsaved changes</span> — Anagram will run on{' '}
                {stagedMode === 'random' ? 'random words' : 'a manual word'} from the next
                session onwards. The current session is untouched.
              </p>
              <div className="flex gap-2">
                <Button
                  type="button"
                  size="sm"
                  onClick={handleSaveMode}
                  disabled={savingMode}
                >
                  {savingMode ? 'Saving...' : 'Save'}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setStagedMode(null);
                    setModeError(null);
                  }}
                >
                  Discard
                </Button>
              </div>
            </div>
          )}

          {modeError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {modeError}
            </p>
          )}

          {formMode === 'custom' ? (
            <div className="flex flex-col gap-2">
              <Label htmlFor="anagram-answer">Word</Label>
              <Input
                id="anagram-answer"
                value={word}
                maxLength={100}
                placeholder="Any word, in any length"
                autoComplete="off"
                onChange={(event) => {
                  setWord(event.target.value);
                  setFormError(null);
                }}
              />
              <p className="text-xs text-muted-foreground">
                Letters only -- no spaces or punctuation. Saved uppercase, so the spelling you
                see is the one that is stored.
              </p>
            </div>
          ) : drawn ? (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border/60 bg-muted/40 p-4">
              <div className="min-w-0">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  Word for today
                </p>
                <p className="break-all font-mono text-2xl font-semibold uppercase tracking-widest">
                  {drawn.word}
                </p>
                <p className="text-xs text-muted-foreground">
                  Drawn by the server. Updating stores this word and no other.
                </p>
              </div>
              <Button type="button" size="sm" onClick={handleDraw} disabled={drawing}>
                <Dices />
                Draw another
              </Button>
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-3">
              <Button type="button" size="sm" variant="outline" onClick={handleDraw} disabled={drawing}>
                <Dices />
                {drawing ? 'Drawing...' : 'Draw a word'}
              </Button>
              <p className="text-xs text-muted-foreground">
                The server picks the word and shows it here before anything is stored.
              </p>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3">
            <Button type="button" onClick={handleSave} disabled={!readyToSave || saving}>
              {saving ? 'Updating...' : 'Update session'}
            </Button>
            <p className="text-xs text-muted-foreground">
              {formMode === 'custom'
                ? 'The word is checked for letters on update.'
                : drawn
                  ? `Replaces the current session with ${drawn.word}.`
                  : 'Draw a word first.'}
            </p>
          </div>

          {formError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {formError}
            </p>
          )}
        </div>

        <div className="flex flex-col gap-3 border-t border-border/40 pt-4">
          <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Schedule a future word
          </h4>
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-2">
              <Label htmlFor="anagram-schedule-date">Period start (UTC)</Label>
              <Input
                id="anagram-schedule-date"
                type="date"
                value={scheduleDate}
                onChange={(event) => {
                  setScheduleDate(event.target.value);
                  setScheduleError(null);
                }}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="anagram-schedule-word">Future word</Label>
              <Input
                id="anagram-schedule-word"
                value={scheduleWord}
                maxLength={100}
                placeholder="Letters only"
                autoComplete="off"
                onChange={(event) => {
                  setScheduleWord(event.target.value);
                  setScheduleError(null);
                }}
              />
            </div>
            <Button
              type="button"
              onClick={handleSchedule}
              disabled={!scheduleDate || !scheduleWord.trim() || scheduling}
            >
              {scheduling ? 'Scheduling...' : 'Schedule word'}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Within 30 days ahead. The word waits until its period opens and never activates
            early.
          </p>
          {scheduleError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {scheduleError}
            </p>
          )}
          {scheduled.length > 0 && (
            <ul className="flex flex-col gap-2">
              {scheduled.map((entry) => (
                <li
                  key={entry.id}
                  className="flex items-center justify-between gap-3 rounded-xl border border-border/60 p-3 text-sm"
                >
                  <span>
                    <span className="font-mono font-semibold">{entry.target_date}</span>
                    {' — '}
                    <span className="break-all font-mono uppercase tracking-widest">
                      {entry.word}
                    </span>
                  </span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => void handleDeleteScheduled(entry.target_date)}
                    aria-label={`Withdraw the word for ${entry.target_date}`}
                  >
                    <X />
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex flex-col gap-3 border-t border-border/40 pt-4">
          <div className="flex items-center justify-between gap-3">
            <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              Session history
            </h4>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">
                {loading ? 'Loading...' : `${games.length} sessions`}
              </span>
              {games.length > 0 && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setHistoryOpen((open) => !open)}
                  aria-expanded={historyOpen}
                  aria-label={historyOpen ? 'Hide session history' : 'Show session history'}
                >
                  {historyOpen ? <ChevronUp /> : <ChevronDown />}
                  {historyOpen ? 'Hide' : 'Show'}
                </Button>
              )}
            </div>
          </div>

          {listError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {listError}
            </p>
          )}

          {!loading && !listError && games.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No sessions yet. The reset loop opens the first one automatically.
            </p>
          )}

          {historyOpen && games.length > 0 && (
            <ul className="flex flex-col gap-2">
              {games.map((game) => (
                <li key={game.id}>
                  <div className="flex items-center justify-between gap-3 rounded-xl border border-border/60 p-3">
                    <button
                      type="button"
                      onClick={() => void openDetail(game.id)}
                      aria-label={`Inspect the session for ${game.game_date}`}
                      className="flex min-w-0 flex-1 flex-wrap items-center gap-3 text-left"
                    >
                      <span className="font-mono text-sm font-semibold">{game.game_date}</span>
                      <Badge variant={game.mode === 'custom' ? 'secondary' : 'default'}>
                        {game.mode === 'custom' ? 'Typed' : 'Drawn'}
                      </Badge>
                      <Badge variant={game.status === 'active' ? 'default' : 'outline'}>
                        {game.status ?? 'active'}
                      </Badge>
                      <span className="break-all font-mono uppercase tracking-widest">
                        {game.answer ?? 'pending'}
                      </span>
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {historyOpen && detail && (
            <div className="rounded-xl border border-border/60 bg-muted/40 p-4 text-sm">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 space-y-1">
                  <p className="font-semibold">Session for {detail.game_date}</p>
                  <p className="break-all font-mono text-xs text-muted-foreground">
                    {detail.id}
                  </p>
                  <p>
                    {detail.mode === 'custom' ? 'Typed' : 'Drawn'} word:{' '}
                    <span className="break-all font-mono uppercase tracking-widest">
                      {detail.answer ?? 'pending'}
                    </span>
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Set by {detail.created_by} on{' '}
                    {new Date(detail.created_at).toLocaleString()} · updated{' '}
                    {new Date(detail.updated_at).toLocaleString()}
                  </p>
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setDetail(null)}
                  aria-label="Close"
                >
                  <X />
                </Button>
              </div>
            </div>
          )}

          {historyOpen && detailError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {detailError}
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
};

export default AdminAnagramConfig;
