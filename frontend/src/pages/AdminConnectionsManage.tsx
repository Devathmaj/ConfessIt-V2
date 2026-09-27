import { useCallback, useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { ArrowLeft, ArrowRight, ChevronDown, ChevronUp, Network, X } from 'lucide-react';
import { toast } from 'sonner';
import { AdminGameStatusControls } from '@/components/AdminGameStatusControls';
import { AdminSetFeaturedCard } from '@/components/AdminSetFeaturedCard';
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
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { describeApiError } from '@/lib/apiError';
import {
  createConnectionsGame,
  deleteScheduledConnectionsPuzzle,
  getAdminConnectionsGames,
  getCurrentAdminConnectionsSession,
  getScheduledConnectionsPuzzles,
  scheduleConnectionsPuzzle,
  type ConnectionsGame,
  type ConnectionsScheduledPuzzle,
} from '@/services/api';

/** Today as UTC `YYYY-MM-DD`, the only calendar the schedule speaks. */
const todayIso = () => new Date().toISOString().slice(0, 10);

const GROUP_NAMES = ['Group 1', 'Group 2', 'Group 3', 'Group 4'];

type GroupsState = [string[], string[], string[], string[]];

const emptyGroups = (): GroupsState => [[], [], [], []].map(() => ['', '', '', '']) as GroupsState;

const groupsFrom = (groups: string[][]): GroupsState =>
  [0, 1, 2, 3].map((index) => {
    const group = groups[index] ?? [];
    return [0, 1, 2, 3].map((slot) => group[slot] ?? '');
  }) as GroupsState;

/** Client-side mirror of the server rules, so problems surface before saving. */
const validateGroups = (groups: GroupsState): string | null => {
  const words = groups.flat();
  if (words.some((word) => word.trim().length === 0)) {
    return 'Fill in all 16 words -- every group needs exactly four.';
  }
  if (words.some((word) => word.trim().length > 50)) {
    return 'Each word is at most 50 characters.';
  }
  const seen = new Map<string, string>();
  for (const word of words) {
    const normalized = word.trim().toLowerCase();
    if (seen.has(normalized)) {
      return `Duplicate word "${word.trim()}": each word may appear only once.`;
    }
    seen.set(normalized, word);
  }
  return null;
};

/**
 * Four group editors for one puzzle: sixteen inputs in four labeled boxes.
 *
 * Words are typed directly into their intended groups. Each filled word also
 * carries a move control that swaps it into another group, so authoring never
 * requires retyping a word to relocate it. Validation errors are reported
 * without clearing anything the administrator typed.
 */
const PuzzleEditor = ({
  idPrefix,
  groups,
  onChange,
  disabled,
}: {
  idPrefix: string;
  groups: GroupsState;
  onChange: (next: GroupsState) => void;
  disabled?: boolean;
}) => {
  const setWord = (groupIndex: number, slotIndex: number, value: string) => {
    onChange(
      groups.map((group, gi) =>
        gi === groupIndex ? group.map((word, si) => (si === slotIndex ? value : word)) : group,
      ) as GroupsState,
    );
  };

  /** Relocate a word: move into a group with room, otherwise swap the two words. */
  const moveWord = (fromGroup: number, fromSlot: number, toGroup: number) => {
    if (toGroup === fromGroup) return;
    const word = groups[fromGroup][fromSlot];
    if (!word) return;
    const target = [...groups[toGroup]];
    const emptySlot = target.findIndex((entry) => entry.trim().length === 0);
    const next = groups.map((group) => [...group]) as GroupsState;
    if (emptySlot >= 0) {
      next[toGroup][emptySlot] = word;
      next[fromGroup][fromSlot] = '';
    } else {
      // Full group: swap with its last word so both groups stay at four.
      const displaced = next[toGroup][3];
      next[toGroup][3] = word;
      next[fromGroup][fromSlot] = displaced;
    }
    onChange(next);
  };

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {groups.map((group, groupIndex) => (
        <fieldset
          key={GROUP_NAMES[groupIndex]}
          className="flex flex-col gap-2 rounded-xl border border-border/60 p-3"
        >
          <legend className="px-1 text-xs font-bold uppercase tracking-wider text-muted-foreground">
            {GROUP_NAMES[groupIndex]}
          </legend>
          {group.map((word, slotIndex) => {
            const inputId = `${idPrefix}-g${groupIndex}-s${slotIndex}`;
            return (
              <div key={slotIndex} className="flex items-center gap-2">
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <Label htmlFor={inputId} className="sr-only">
                    {GROUP_NAMES[groupIndex]} word {slotIndex + 1}
                  </Label>
                  <Input
                    id={inputId}
                    value={word}
                    maxLength={50}
                    placeholder={`Word ${slotIndex + 1}`}
                    autoComplete="off"
                    disabled={disabled}
                    onChange={(event) => setWord(groupIndex, slotIndex, event.target.value)}
                  />
                </div>
                <label className="sr-only" htmlFor={`${inputId}-move`}>
                  Move this word to another group
                </label>
                <select
                  id={`${inputId}-move`}
                  aria-label={`Move ${word || `word ${slotIndex + 1}`} to another group`}
                  disabled={disabled || word.trim().length === 0}
                  defaultValue=""
                  onChange={(event) => {
                    const toGroup = Number(event.target.value);
                    event.target.value = '';
                    if (!Number.isNaN(toGroup)) moveWord(groupIndex, slotIndex, toGroup);
                  }}
                  className="h-9 shrink-0 rounded-md border border-border/60 bg-background px-1 text-xs text-muted-foreground disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <option value="" disabled>
                    Move…
                  </option>
                  {GROUP_NAMES.map((name, index) =>
                    index === groupIndex ? null : (
                      <option key={name} value={index}>
                        → {name}
                      </option>
                    ),
                  )}
                </select>
              </div>
            );
          })}
        </fieldset>
      ))}
    </div>
  );
};

/**
 * The Connections console: the current session's puzzle, future puzzles, and
 * history -- plus this game's own availability and reset cadence.
 *
 * "Update session" ends the current session and opens a brand-new one with
 * the typed puzzle; history and player progress stay attached to the old
 * session. Future puzzles are scheduled inside the finite horizon and wait
 * until their period opens. Connections has no random-content mode: a session
 * with no valid puzzle stays pending and unplayable rather than drawing
 * anything.
 */
export const AdminConnectionsManage = () => {
  const { logActivity } = useGamesConfig();

  const [current, setCurrent] = useState<ConnectionsGame | null>(null);
  const [games, setGames] = useState<ConnectionsGame[]>([]);
  const [scheduled, setScheduled] = useState<ConnectionsScheduledPuzzle[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [editor, setEditor] = useState<GroupsState>(emptyGroups);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [scheduleDate, setScheduleDate] = useState<string>(todayIso);
  const [scheduleEditor, setScheduleEditor] = useState<GroupsState>(emptyGroups);
  const [scheduling, setScheduling] = useState(false);
  const [scheduleError, setScheduleError] = useState<string | null>(null);

  // Session history stays collapsed so a long season of past sessions does not
  // push the working controls off the screen.
  const [historyOpen, setHistoryOpen] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setGames(await getAdminConnectionsGames());
      setListError(null);
    } catch (error) {
      setListError(describeApiError(error, 'Could not load the configured puzzles.'));
    } finally {
      setLoading(false);
    }
    try {
      const session = await getCurrentAdminConnectionsSession();
      setCurrent(session);
      if (session.groups.length === 4) {
        setEditor(groupsFrom(session.groups));
      }
    } catch {
      setCurrent(null);
    }
    try {
      setScheduled(await getScheduledConnectionsPuzzles());
      setScheduleError(null);
    } catch (error) {
      setScheduleError(describeApiError(error, 'Could not load the scheduled puzzles.'));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleSave = async () => {
    const problem = validateGroups(editor);
    if (problem) {
      setFormError(problem);
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      const created = await createConnectionsGame({ groups: editor.map((group) => [...group]) });
      toast.success(`Connections session updated for ${created.game_date}.`);
      logActivity({
        kind: 'puzzle',
        gameId: 'connections',
        message: `Connections session updated for ${created.game_date}`,
      });
      await refresh();
    } catch (error) {
      setFormError(describeApiError(error, 'Could not update the session.'));
    } finally {
      setSaving(false);
    }
  };

  const handleSchedule = async () => {
    if (!scheduleDate) return;
    const problem = validateGroups(scheduleEditor);
    if (problem) {
      setScheduleError(problem);
      return;
    }
    setScheduling(true);
    setScheduleError(null);
    try {
      const created = await scheduleConnectionsPuzzle(
        scheduleDate,
        scheduleEditor.map((group) => [...group]),
      );
      toast.success(`Connections puzzle scheduled for ${created.target_date}.`);
      setScheduleEditor(emptyGroups());
      await refresh();
    } catch (error) {
      setScheduleError(describeApiError(error, 'Could not schedule the puzzle.'));
    } finally {
      setScheduling(false);
    }
  };

  const handleDeleteScheduled = async (targetDate: string) => {
    try {
      await deleteScheduledConnectionsPuzzle(targetDate);
      toast.info(`Scheduled puzzle for ${targetDate} withdrawn.`);
      await refresh();
    } catch (error) {
      setScheduleError(describeApiError(error, 'Could not withdraw the puzzle.'));
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-2">
          <NavLink
            to="/admin/games/manage"
            className="inline-flex w-fit items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="h-3 w-3" />
            Back to Manage games
          </NavLink>
          <h1 className="text-3xl font-semibold">Connections puzzles</h1>
          <p className="text-sm text-muted-foreground">
            Sixteen words in four hidden groups of four. Updating ends the
            current session and opens a new one -- history keeps its own answer
            key. Group names are organizational only and are never shown to
            players.
          </p>
        </div>
        {current && (
          <Badge variant={current.status === 'pending' ? 'destructive' : 'default'}>
            {current.status === 'pending' ? 'Pending — puzzle not set' : 'Active'}
          </Badge>
        )}
      </header>

      <AdminSetFeaturedCard gameId="connections" gameName="Connections" />

      <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-xl">
            <Network className="h-5 w-5" />
            Connections
          </CardTitle>
          <CardDescription>
            Type sixteen distinct words into their four intended groups, or move
            words between groups while authoring. Saving validates without
            clearing anything typed.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <AdminGameStatusControls gameId="connections" gameName="Connections" />

          {current && (
            <p className="rounded-xl border border-border/60 bg-muted/40 p-3 text-sm">
              Current session:{' '}
              <span className="font-mono font-semibold">
                {current.groups.length === 4
                  ? `${current.groups.flat().length} words in 4 groups`
                  : 'no puzzle yet'}
              </span>{' '}
              <span className="text-muted-foreground">
                ({current.game_date}
                {current.period_end_date ? ` → ${current.period_end_date}` : ''})
              </span>
            </p>
          )}

          <div className="flex flex-col gap-4">
            <PuzzleEditor
              idPrefix="connections-now"
              groups={editor}
              onChange={(next) => {
                setEditor(next);
                setFormError(null);
              }}
              disabled={saving}
            />
            <div className="flex flex-wrap items-center gap-3">
              <Button type="button" onClick={handleSave} disabled={saving}>
                {saving ? 'Updating...' : 'Update session'}
              </Button>
              <p className="text-xs text-muted-foreground">
                Ends the current session and opens a new one with this puzzle.
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
              Schedule a future puzzle
            </h4>
            <div className="flex flex-col gap-2">
              <Label htmlFor="connections-schedule-date">Period start (UTC)</Label>
              <Input
                id="connections-schedule-date"
                type="date"
                value={scheduleDate}
                className="max-w-xs"
                onChange={(event) => {
                  setScheduleDate(event.target.value);
                  setScheduleError(null);
                }}
              />
            </div>
            <PuzzleEditor
              idPrefix="connections-future"
              groups={scheduleEditor}
              onChange={(next) => {
                setScheduleEditor(next);
                setScheduleError(null);
              }}
              disabled={scheduling}
            />
            <div className="flex flex-wrap items-center gap-3">
              <Button type="button" onClick={handleSchedule} disabled={!scheduleDate || scheduling}>
                {scheduling ? 'Scheduling...' : 'Schedule puzzle'}
              </Button>
              <p className="text-xs text-muted-foreground">
                Within 30 days ahead. The puzzle waits until its period opens
                and never activates early.
              </p>
            </div>
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
                      <span className="text-muted-foreground">
                        {entry.groups.flat().length} words in {entry.groups.length} groups
                      </span>
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => void handleDeleteScheduled(entry.target_date)}
                      aria-label={`Withdraw the puzzle for ${entry.target_date}`}
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
                  <li
                    key={game.id}
                    className="flex flex-wrap items-center gap-3 rounded-xl border border-border/60 p-3 text-sm"
                  >
                    <span className="font-mono font-semibold">{game.game_date}</span>
                    <Badge variant={game.status === 'active' ? 'default' : 'outline'}>
                      {game.status ?? 'active'}
                    </Badge>
                    <span className="text-muted-foreground">
                      {game.groups.length === 4
                        ? `${game.groups.flat().length} words in 4 groups`
                        : 'pending — no puzzle'}
                    </span>
                    <span className="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
                      Set by {game.created_by}
                      <ArrowRight className="h-3 w-3" />
                      {game.period_end_date ?? ''}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
};

export default AdminConnectionsManage;
