import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  KeyRound,
  Loader2,
  RefreshCw,
  Users,
} from 'lucide-react';
import { toast } from 'sonner';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useAuth } from '@/contexts/AuthContext';
import { describeApiError } from '@/lib/apiError';
import { cn } from '@/lib/utils';
import {
  changeTwoKeyEscapeRole,
  getTwoKeyEscapeLeaderboard,
  getTwoKeyEscapePartners,
  getTwoKeyEscapeState,
  moveTwoKeyEscape,
  selectTwoKeyEscapePartner,
  spendTwoKeyEscapeKey,
  submitTwoKeyEscapeRole,
  type CouplePartnerCandidate,
  type TwoKeyEscapeDirection,
  type TwoKeyEscapeLeaderboardEntry,
  type TwoKeyEscapeRole,
  type TwoKeyEscapeState,
} from '@/services/api';

const ROLE_LABELS: Record<TwoKeyEscapeRole, string> = {
  navigator: 'Navigator',
  key_operator: 'Key Operator',
};

const ROLE_BLURBS: Record<TwoKeyEscapeRole, string> = {
  navigator: 'Move the character through the maze, one cell at a time.',
  key_operator: 'Spend keys to unlock cells and reshape the maze.',
};

/** Render start-to-finish seconds as a compact duration ("2h 5m", "3m 12s"). */
const formatElapsed = (totalSeconds: number | null): string => {
  if (totalSeconds === null || totalSeconds === undefined) return '—';
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${rest}s`;
  return `${rest}s`;
};

/**
 * Two-Key Escape: one shared maze, two complementary roles.
 *
 * The screen never invents state: it reads the caller's state on mount and
 * after every write, and renders whichever phase the server reports -- partner
 * choice, role selection, conflict, playing, or the settled result. Choosing a
 * partner here is the Questionnaire's own mutual selection, so a pair formed
 * here opens that game too. Role option clicks stay local until confirmed;
 * only the confirmations travel over HTTP. While waiting on the partner (a
 * missing pick, submission, conflict, or the partner's moves and keys) the
 * state re-reads on an interval, the same realtime the Couple Questionnaire
 * uses -- no game owns a WebSocket, only chat streams over Redis.
 *
 * Nothing is shown as committed before the backend accepts it: every action
 * awaits the response and renders that, and a refusal toasts the server's
 * sentence and re-reads rather than rolling anything forward.
 */
const sameGenderOf = (mine: string | undefined, candidate: string) =>
  mine ? candidate === mine : true;

const defaultVisibleOf = (mine: string | undefined, candidate: string) => {
  if (!mine || mine === 'other') return true;
  if (mine === 'male') return candidate === 'female' || candidate === 'other';
  if (mine === 'female') return candidate === 'male' || candidate === 'other';
  return true;
};

export const TwoKeyEscapePage = () => {
  const { user } = useAuth();
  const myGender = user?.gender;

  const [state, setState] = useState<TwoKeyEscapeState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [acting, setActing] = useState(false);

  // Local-only role pick: clicking an option issues no request. Only the
  // explicit confirm below sends the submission to the server.
  const [localRole, setLocalRole] = useState<TwoKeyEscapeRole | null>(null);
  const [changingRole, setChangingRole] = useState(false);

  // Partner discovery, settled exactly like the Couple Questionnaire: the
  // list and the choice below are that game's own pairing, so a pair formed
  // here opens the Questionnaire too, and vice versa.
  const [partners, setPartners] = useState<CouplePartnerCandidate[]>([]);
  const [partnersError, setPartnersError] = useState<string | null>(null);
  const [showSameGender, setShowSameGender] = useState(false);
  const [search, setSearch] = useState('');
  const [choosingId, setChoosingId] = useState<string | null>(null);

  const [leaderboard, setLeaderboard] = useState<TwoKeyEscapeLeaderboardEntry[]>([]);
  const [cooldown, setCooldown] = useState(0);

  const refresh = useCallback(async () => {
    try {
      const next = await getTwoKeyEscapeState();
      setState(next);
      setLoadError(null);
      setCooldown(Math.ceil(next.cooldown_seconds_remaining ?? 0));
      // A resolved conflict or finalized assignment replaces the local pick.
      if (next.roles_finalized || !next.conflict) {
        setChangingRole(false);
      }
      return next;
    } catch (error) {
      setLoadError(describeApiError(error, 'Could not load the game.'));
      return null;
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const refreshLeaderboard = useCallback(async () => {
    try {
      setLeaderboard(await getTwoKeyEscapeLeaderboard());
    } catch {
      // Standings are secondary; the board is the source of truth.
    }
  }, []);

  useEffect(() => {
    if (state?.stage === 'playing' || state?.stage === 'completed') {
      void refreshLeaderboard();
    }
  }, [state?.stage, state?.state_version, refreshLeaderboard]);

  const partnerStage = state?.stage;
  useEffect(() => {
    if (partnerStage !== 'no_couple') return;
    let cancelled = false;
    void (async () => {
      try {
        const list = await getTwoKeyEscapePartners();
        if (!cancelled) {
          setPartners(list);
          setPartnersError(null);
        }
      } catch (error) {
        if (!cancelled) {
          setPartnersError(describeApiError(error, 'Could not load people to choose from.'));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [partnerStage]);

  const visiblePartners = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return partners.filter((candidate) => {
      const genderOk = showSameGender
        ? sameGenderOf(myGender, candidate.gender)
        : defaultVisibleOf(myGender, candidate.gender);
      if (!genderOk) return false;
      if (needle && !candidate.name.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [partners, showSameGender, myGender, search]);

  const handleChoose = async (candidateId: string) => {
    setChoosingId(candidateId);
    try {
      setState(await selectTwoKeyEscapePartner(candidateId));
      toast.success('Choice saved. The maze opens when they choose you back.');
    } catch (error) {
      toast.error(describeApiError(error, 'Could not save your choice.'));
    } finally {
      setChoosingId(null);
    }
  };

  // While the game is open, re-read on the same 15s interval the Couple
  // Questionnaire polls on: the server unlocks the next phase the moment both
  // sides have acted, and this surfaces it without a manual check.
  useEffect(() => {
    if (
      state?.stage !== 'no_couple' &&
      state?.stage !== 'selecting_roles' &&
      state?.stage !== 'playing'
    )
      return;
    let inFlight = false;
    const timer = setInterval(() => {
      if (inFlight) return;
      inFlight = true;
      void refresh().finally(() => {
        inFlight = false;
      });
    }, 15000);
    return () => clearInterval(timer);
  }, [state?.stage, refresh]);

  // The key cooldown ticks locally for display; the server enforces it.
  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = setInterval(() => {
      setCooldown((value) => {
        if (value <= 1) {
          void refresh();
          return 0;
        }
        return value - 1;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [cooldown, refresh]);

  const runAction = useCallback(
    async (action: () => Promise<TwoKeyEscapeState>, failureHint: string) => {
      setActing(true);
      try {
        const next = await action();
        setState(next);
        setCooldown(Math.ceil(next.cooldown_seconds_remaining ?? 0));
        if (next.roles_finalized || !next.conflict) setChangingRole(false);
      } catch (error) {
        toast.error(describeApiError(error, failureHint));
        await refresh();
      } finally {
        setActing(false);
      }
    },
    [refresh],
  );

  const handleConfirmRole = () => {
    if (!localRole) return;
    void runAction(() => submitTwoKeyEscapeRole(localRole), 'Could not save your role.');
  };

  const handleConfirmChange = () => {
    if (!localRole) return;
    void runAction(() => changeTwoKeyEscapeRole(localRole), 'Could not change your role.');
  };

  const handleMove = (direction: TwoKeyEscapeDirection) => {
    void runAction(() => moveTwoKeyEscape(direction), 'Could not move.');
  };

  const handleKey = (keyIndex: number) => {
    void runAction(() => spendTwoKeyEscapeKey(keyIndex), 'Could not use that key.');
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    await refresh();
  };

  // Arrow keys are a convenience for the Navigator only: ignored while typing
  // in an input or textarea, and never bypassing backend validation.
  const stateRef = useRef(state);
  stateRef.current = state;
  const actingRef = useRef(acting);
  actingRef.current = acting;
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const current = stateRef.current;
      if (!current || current.stage !== 'playing' || current.my_role !== 'navigator') return;
      if (actingRef.current) return;
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA')) return;
      const mapping: Record<string, TwoKeyEscapeDirection> = {
        ArrowUp: 'up',
        ArrowDown: 'down',
        ArrowLeft: 'left',
        ArrowRight: 'right',
      };
      const direction = mapping[event.key];
      if (!direction) return;
      event.preventDefault();
      const snapshot = stateRef.current;
      if (!snapshot || snapshot.stage !== 'playing' || actingRef.current) return;
      actingRef.current = true;
      void (async () => {
        try {
          const next = await moveTwoKeyEscape(direction);
          setState(next);
        } catch (error) {
          toast.error(describeApiError(error, 'Could not move.'));
          await refresh();
        } finally {
          actingRef.current = false;
        }
      })();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [refresh]);

  if (loading) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Games"
          title="Two-Key Escape"
          description="One maze, two roles — navigate together, asynchronously."
        />
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading the game…
        </p>
      </div>
    );
  }

  if (loadError || state === null) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Games"
          title="Two-Key Escape"
          description="One maze, two roles — navigate together, asynchronously."
        />
        <p role="alert" className="text-sm font-medium text-destructive">
          {loadError ?? 'Could not load the game.'}
        </p>
        <Button type="button" variant="outline" size="sm" className="mt-3" onClick={handleRefresh}>
          <RefreshCw />
          Try again
        </Button>
      </div>
    );
  }

  const isNavigator = state.my_role === 'navigator';
  const isOperator = state.my_role === 'key_operator';

  return (
    <div>
      <GamesPageHeader
        eyebrow="Games"
        title="Two-Key Escape"
        description="One maze, two roles — navigate together, asynchronously."
      />

      {state.stage === 'no_couple' && (
        <Card className="rounded-2xl border-border/60 bg-card/70">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-xl">
              <Users className="h-5 w-5" />
              Choose your partner
            </CardTitle>
            <CardDescription>
              {state.my_selection
                ? `You chose ${state.my_selection.selected_user_name}. Choosing someone else replaces your pick. The maze opens when they choose you back — nobody is notified.`
                : 'Nothing starts until the two of you have chosen each other. Nobody is notified of your pick.'}{' '}
              This is the Couple Questionnaire pairing itself: a pair formed here opens that
              game too, and vice versa.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {state.my_selection && (
              <p className="rounded-xl border border-border/60 bg-muted/40 p-3 text-sm">
                Current pick:{' '}
                <span className="font-semibold">{state.my_selection.selected_user_name}</span>
              </p>
            )}

            <div className="flex flex-wrap items-end gap-3">
              <div className="flex min-w-44 flex-1 flex-col gap-2">
                <Label htmlFor="escape-partner-search">Find someone</Label>
                <Input
                  id="escape-partner-search"
                  value={search}
                  placeholder="Type a name"
                  autoComplete="off"
                  onChange={(event) => setSearch(event.target.value)}
                />
              </div>
              <Button
                type="button"
                variant={showSameGender ? 'default' : 'outline'}
                size="sm"
                aria-pressed={showSameGender}
                onClick={() => setShowSameGender((value) => !value)}
              >
                {showSameGender ? 'Showing same gender' : 'Show same gender'}
              </Button>
            </div>

            {partnersError && (
              <p role="alert" className="text-sm font-medium text-destructive">
                {partnersError}
              </p>
            )}

            {!partnersError && visiblePartners.length === 0 && (
              <p className="text-sm text-muted-foreground">
                Nobody matches that filter yet. Try the search or the gender toggle.
              </p>
            )}

            {visiblePartners.length > 0 && (
              <ul className="flex flex-col gap-2">
                {visiblePartners.map((candidate) => {
                  const isCurrent = state.my_selection?.selected_user_id === candidate.id;
                  return (
                    <li
                      key={candidate.id}
                      className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border/60 p-3"
                    >
                      <div className="min-w-0">
                        <p className="truncate font-semibold">{candidate.name}</p>
                        <p className="truncate text-xs capitalize text-muted-foreground">
                          {candidate.gender} · {candidate.department} · Year {candidate.year}
                        </p>
                      </div>
                      <div className="flex items-center gap-2">
                        {isCurrent && <Badge variant="secondary">Your pick</Badge>}
                        <Button
                          type="button"
                          size="sm"
                          variant={isCurrent ? 'ghost' : 'outline'}
                          disabled={choosingId !== null}
                          onClick={() => void handleChoose(candidate.id)}
                          aria-label={`Choose ${candidate.name} as partner`}
                        >
                          {choosingId === candidate.id
                            ? 'Saving…'
                            : isCurrent
                              ? 'Chosen'
                              : 'Choose'}
                        </Button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      {state.stage === 'selecting_roles' && (
        <RoleSelectionCard
          state={state}
          localRole={localRole}
          changingRole={changingRole}
          acting={acting}
          onPick={setLocalRole}
          onConfirm={handleConfirmRole}
          onStartChange={() => {
            setChangingRole(true);
            setLocalRole(
              state.my_submitted_role === 'navigator' ? 'key_operator' : 'navigator',
            );
          }}
          onCancelChange={() => {
            setChangingRole(false);
            setLocalRole(null);
          }}
          onConfirmChange={handleConfirmChange}
        />
      )}

      {(state.stage === 'playing' || state.stage === 'completed') && (
        <div className="flex flex-col gap-4">
          <Card className="rounded-2xl border-border/60 bg-card/70">
            <CardContent className="flex flex-wrap items-center gap-2 pt-6">
              <Badge variant={isNavigator ? 'default' : 'secondary'}>
                You: {state.my_role ? ROLE_LABELS[state.my_role] : '—'}
              </Badge>
              <Badge variant="outline">
                Partner: {state.partner?.name ?? 'Partner'}
              </Badge>
              <Badge variant="secondary">
                {state.grid_size}×{state.grid_size} · {state.keys_remaining} of{' '}
                {state.key_charges} keys left
              </Badge>
              {state.stage === 'completed' ? (
                <Badge variant="default">Escaped</Badge>
              ) : (
                <Badge variant="outline">
                  Moves: {state.total_moves} · Keys: {state.total_keys}
                </Badge>
              )}
            </CardContent>
          </Card>

          <MazeGrid state={state} />

          {state.stage === 'playing' && isNavigator && (
            <Card className="rounded-2xl border-border/60 bg-card/70">
              <CardHeader>
                <CardTitle className="text-lg">Move</CardTitle>
                <CardDescription>
                  One cell per move. Arrow keys work too while this page is active.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="mx-auto grid w-fit grid-cols-3 gap-2" role="group" aria-label="Move">
                  <span />
                  <Button
                    type="button"
                    variant="outline"
                    size="lg"
                    disabled={acting}
                    onClick={() => handleMove('up')}
                    aria-label="Move up"
                  >
                    <ArrowUp />
                  </Button>
                  <span />
                  <Button
                    type="button"
                    variant="outline"
                    size="lg"
                    disabled={acting}
                    onClick={() => handleMove('left')}
                    aria-label="Move left"
                  >
                    <ArrowLeft />
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="lg"
                    disabled={acting}
                    onClick={() => handleMove('down')}
                    aria-label="Move down"
                  >
                    <ArrowDown />
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="lg"
                    disabled={acting}
                    onClick={() => handleMove('right')}
                    aria-label="Move right"
                  >
                    <ArrowRight />
                  </Button>
                </div>
              </CardContent>
            </Card>
          )}

          {state.stage === 'playing' && isOperator && (
            <Card className="rounded-2xl border-border/60 bg-card/70">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-lg">
                  <KeyRound className="h-5 w-5" />
                  Keys
                </CardTitle>
                <CardDescription>
                  Each key unlocks its locked cell and blocks another cell elsewhere. Ten
                  seconds between keys, and the Navigator must move before the next one.
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {cooldown > 0 && (
                  <p role="status" className="text-sm font-medium">
                    Cooling down: {cooldown}s before the next key.
                  </p>
                )}
                {state.movement_required && (
                  <p role="status" className="text-sm font-medium text-amber-600 dark:text-amber-400">
                    The Navigator must move at least once before the next key.
                  </p>
                )}
                <div className="grid gap-2" role="group" aria-label="Keys">
                  {state.keys.map((key) => {
                    const disabled =
                      acting || key.used || cooldown > 0 || state.movement_required;
                    return (
                      <Button
                        key={key.key_index}
                        type="button"
                        variant={key.used ? 'ghost' : 'outline'}
                        disabled={disabled}
                        className="justify-start"
                        onClick={() => handleKey(key.key_index)}
                        aria-label={`Use key ${key.key_index + 1} on row ${key.target_row + 1}, column ${key.target_col + 1}`}
                      >
                        <KeyRound className="h-4 w-4" />
                        Key {key.key_index + 1} → row {key.target_row + 1}, column{' '}
                        {key.target_col + 1}
                        {key.used && ' · used'}
                      </Button>
                    );
                  })}
                </div>
              </CardContent>
            </Card>
          )}

          {state.stage === 'playing' && !isNavigator && !isOperator && (
            <p className="text-sm text-muted-foreground">
              Roles are still finalizing — this page updates by itself.
            </p>
          )}

          {state.stage === 'completed' && state.result && (
            <Card className="rounded-2xl border-border/60 bg-card/70">
              <CardHeader>
                <CardTitle className="text-xl">You escaped together</CardTitle>
                <CardDescription>
                  Ranked by start-to-finish time, from the first move to the exit.
                  Time away counts.
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-wrap gap-3">
                <div className="min-w-28 flex-1 rounded-xl border border-border/60 p-4 text-center">
                  <p className="text-xs uppercase tracking-wide text-muted-foreground">Time taken</p>
                  <p className="text-2xl font-bold tabular-nums">
                    {formatElapsed(state.result.elapsed_seconds)}
                  </p>
                </div>
                <ResultStat label="Valid moves" value={state.result.total_moves} />
                <ResultStat label="Keys used" value={state.result.total_keys} />
                <ResultStat label="Score" value={state.result.score} />
              </CardContent>
            </Card>
          )}

          <Card className="rounded-2xl border-border/60 bg-card/70">
            <CardHeader>
              <CardTitle className="text-lg">Standings</CardTitle>
              <CardDescription>
                Completed couples rank above the rest; the fastest time wins.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-2">
              {leaderboard.length === 0 && (
                <p className="text-sm text-muted-foreground">No standings yet.</p>
              )}
              {leaderboard.map((entry) => (
                <div
                  key={entry.rank}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border/60 p-3 text-sm"
                >
                  <span className="font-semibold">
                    #{entry.rank} {entry.couple_names.join(' & ')}
                  </span>
                  <span className="text-muted-foreground">
                    {entry.completed
                      ? `${formatElapsed(entry.elapsed_seconds)} · ${entry.score} pts`
                      : 'still finding the exit'}
                  </span>
                </div>
              ))}
            </CardContent>
          </Card>
        </div>
      )}

      <div className="mt-4">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={handleRefresh}
          disabled={refreshing}
        >
          <RefreshCw className={cn(refreshing && 'animate-spin')} />
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </Button>
      </div>
    </div>
  );
};

const ResultStat = ({ label, value }: { label: string; value: number }) => (
  <div className="min-w-28 flex-1 rounded-xl border border-border/60 p-4 text-center">
    <p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p>
    <p className="text-2xl font-bold tabular-nums">{value.toLocaleString()}</p>
  </div>
);

const RoleSelectionCard = ({
  state,
  localRole,
  changingRole,
  acting,
  onPick,
  onConfirm,
  onStartChange,
  onCancelChange,
  onConfirmChange,
}: {
  state: TwoKeyEscapeState;
  localRole: TwoKeyEscapeRole | null;
  changingRole: boolean;
  acting: boolean;
  onPick: (role: TwoKeyEscapeRole) => void;
  onConfirm: () => void;
  onStartChange: () => void;
  onCancelChange: () => void;
  onConfirmChange: () => void;
}) => {
  const effectivePick = localRole ?? state.my_submitted_role;
  const submittedLabel = (role: TwoKeyEscapeRole | null) =>
    role ? ROLE_LABELS[role] : 'not yet';
  return (
    <Card className="rounded-2xl border-border/60 bg-card/70">
      <CardHeader>
        <CardTitle className="text-xl">Choose your role</CardTitle>
        <CardDescription>
          {state.partner
            ? `Playing with ${state.partner.name}. Picking an option saves nothing yet — confirm to submit.`
            : 'Picking an option saves nothing yet — confirm to submit.'}{' '}
          The game begins once your confirmed roles differ.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="grid gap-2 sm:grid-cols-2" role="group" aria-label="Roles">
          {(Object.keys(ROLE_LABELS) as TwoKeyEscapeRole[]).map((role) => {
            const selected = effectivePick === role;
            return (
              <button
                key={role}
                type="button"
                aria-pressed={selected}
                onClick={() => onPick(role)}
                className={cn(
                  'rounded-xl border p-4 text-left transition-colors',
                  selected
                    ? 'border-transparent bg-gradient-romantic text-white'
                    : 'border-border/60 hover:border-romantic/50',
                )}
              >
                <span className="block font-semibold">{ROLE_LABELS[role]}</span>
                <span className={cn('block text-sm', !selected && 'text-muted-foreground')}>
                  {ROLE_BLURBS[role]}
                </span>
              </button>
            );
          })}
        </div>

        <p className="text-sm text-muted-foreground">
          You confirmed: <span className="font-semibold">{submittedLabel(state.my_submitted_role)}</span>
          {' · '}
          {state.partner?.name ?? 'Partner'} confirmed:{' '}
          <span className="font-semibold">{submittedLabel(state.partner_submitted_role)}</span>
        </p>

        {!state.my_submitted_role && (
          <div>
            <Button
              type="button"
              disabled={!localRole || acting}
              onClick={onConfirm}
            >
              {acting ? 'Saving…' : 'Confirm role'}
            </Button>
          </div>
        )}

        {state.conflict && (
          <div role="alert" className="rounded-xl border border-destructive/50 bg-destructive/10 p-4">
            <p className="text-sm font-medium">{state.conflict}</p>
            {!changingRole ? (
              <Button type="button" variant="outline" size="sm" className="mt-3" onClick={onStartChange}>
                Change Role
              </Button>
            ) : (
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <Button type="button" size="sm" disabled={!localRole || acting} onClick={onConfirmChange}>
                  {acting ? 'Saving…' : `Confirm ${localRole ? ROLE_LABELS[localRole] : 'role'}`}
                </Button>
                <Button type="button" variant="ghost" size="sm" onClick={onCancelChange}>
                  Cancel
                </Button>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
};

const MazeGrid = ({ state }: { state: TwoKeyEscapeState }) => {
  const size = state.grid_size ?? 0;
  if (!size || state.player_row === null || state.player_col === null) return null;
  const wallSet = new Set(state.walls.map(([r, c]) => `${r}:${c}`));
  const lockedSet = new Set(state.locked_cells.map(([r, c]) => `${r}:${c}`));
  const blockedSet = new Set(state.blocked_cells.map(([r, c]) => `${r}:${c}`));
  const cells = [];
  for (let row = 0; row < size; row += 1) {
    for (let col = 0; col < size; col += 1) {
      const key = `${row}:${col}`;
      const isPlayer = row === state.player_row && col === state.player_col;
      const isExit = row === state.exit_row && col === state.exit_col;
      const isWall = wallSet.has(key);
      const isLocked = lockedSet.has(key);
      const isBlocked = blockedSet.has(key);
      cells.push(
        <div
          key={key}
          role="img"
          aria-label={
            isPlayer
              ? `player at row ${row + 1} column ${col + 1}`
              : isExit
                ? 'exit'
                : isWall
                  ? 'wall'
                  : isLocked
                    ? 'locked cell'
                    : isBlocked
                      ? 'blocked cell'
                      : `open cell row ${row + 1} column ${col + 1}`
          }
          className={cn(
            'flex aspect-square items-center justify-center rounded-md border text-xs font-bold',
            isPlayer && 'border-transparent bg-gradient-romantic text-white',
            !isPlayer && isExit && 'border-emerald-500 bg-emerald-500/20 text-emerald-600',
            !isPlayer && !isExit && isWall && 'border-border bg-muted-foreground/30',
            !isPlayer && !isExit && !isWall && isLocked && 'border-amber-500 bg-amber-500/20 text-amber-600',
            !isPlayer && !isExit && !isWall && !isLocked && isBlocked && 'border-red-500 bg-red-500/20 text-red-500',
            !isPlayer && !isExit && !isWall && !isLocked && !isBlocked && 'border-border/60 bg-background/40',
          )}
        >
          {isPlayer ? '●' : isExit ? '⚑' : isLocked ? '🔒' : ''}
        </div>,
      );
    }
  }
  return (
    <Card className="rounded-2xl border-border/60 bg-card/70">
      <CardContent className="pt-6">
        <div
          className="mx-auto grid w-full max-w-md gap-1"
          style={{ gridTemplateColumns: `repeat(${size}, minmax(0, 1fr))` }}
          aria-label="Maze"
        >
          {cells}
        </div>
        <p className="mt-3 text-center text-xs text-muted-foreground">
          ● you · ⚑ exit · 🔒 locked · dark: wall · red: blocked
        </p>
      </CardContent>
    </Card>
  );
};

export default TwoKeyEscapePage;
