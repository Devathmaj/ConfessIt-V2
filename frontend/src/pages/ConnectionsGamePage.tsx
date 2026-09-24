import { useCallback, useEffect, useRef, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { CheckCircle2, Loader2, RotateCcw } from 'lucide-react';
import { toast } from 'sonner';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { describeApiError } from '@/lib/apiError';
import { cn } from '@/lib/utils';
import {
  assignWord,
  emptyAssignment,
  isComplete,
  submissionGroups,
  unassignedCount,
  type ConnectionsAssignment,
} from '@/lib/connectionsGroups';
import {
  getCurrentConnections,
  submitConnections,
  type ConnectionsCurrentGame,
} from '@/services/api';

/**
 * The daily Connections: sixteen tiles, four hidden groups, one submission.
 *
 * The screen reads today's puzzle once on mount and never invents state
 * itself: the tile order is dealt by the server per player and stays stable
 * across refreshes, so returning shows the same board. Arranging tiles stores
 * nothing -- only the explicit final submit consumes the one submission, and
 * a submitted player reads a confirmation rather than a board.
 *
 * The canonical groups never travel here. The tiles hold display spellings
 * only, the four destination areas are semantically unlabeled (Group 1-4),
 * and the submit response carries no score, no correctness and no membership.
 */

const GROUP_LABELS = ['Group 1', 'Group 2', 'Group 3', 'Group 4'];

export const ConnectionsGamePage = () => {
  const [game, setGame] = useState<ConnectionsCurrentGame | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [assignment, setAssignment] = useState<ConnectionsAssignment | null>(null);
  const [selectedWord, setSelectedWord] = useState<string | null>(null);
  const [dragWord, setDragWord] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // Two-step submit: the first press arms the confirmation, the second press
  // sends it -- so an accidental click never locks the puzzle.
  const [confirmArmed, setConfirmArmed] = useState(false);
  const gameIdRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getCurrentConnections()
      .then((data) => {
        if (cancelled) return;
        gameIdRef.current = data.game_id;
        setGame(data);
        if (data.game_id && !data.submitted && data.words.length > 0) {
          setAssignment(emptyAssignment(data.words));
        }
      })
      .catch((error) => {
        if (!cancelled) setLoadError(describeApiError(error, "Could not load today's puzzle."));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const moveWord = useCallback((word: string, destination: number) => {
    setAssignment((previous) => {
      if (!previous) return previous;
      return assignWord(previous, word, destination);
    });
    setSelectedWord(null);
    setConfirmArmed(false);
  }, []);

  const onTileClick = (word: string) => {
    if (!assignment) return;
    setConfirmArmed(false);
    if (selectedWord === null) {
      setSelectedWord(word);
      return;
    }
    if (selectedWord === word) {
      setSelectedWord(null);
      return;
    }
    // A second tile tap while one is selected: no-op -- tiles are not
    // destinations. Keep the newest selection so touch play stays fluid.
    setSelectedWord(word);
  };

  const onDestinationClick = (destination: number) => {
    if (selectedWord !== null) {
      moveWord(selectedWord, destination);
    }
  };

  const onDragStart = (event: React.DragEvent, word: string) => {
    event.dataTransfer.setData('text/plain', word);
    event.dataTransfer.effectAllowed = 'move';
    setDragWord(word);
    setConfirmArmed(false);
  };

  const onDragEnd = () => {
    setDragWord(null);
    setDropTarget(null);
  };

  const onDropOn = (event: React.DragEvent, destination: number) => {
    event.preventDefault();
    const word = event.dataTransfer.getData('text/plain') || dragWord;
    setDropTarget(null);
    setDragWord(null);
    if (word) moveWord(word, destination);
  };

  const resetBoard = () => {
    if (!game || game.words.length === 0) return;
    setAssignment(emptyAssignment(game.words));
    setSelectedWord(null);
    setConfirmArmed(false);
  };

  const submitFinal = async () => {
    if (!assignment || !game?.game_id || submitting) return;
    if (!isComplete(assignment)) {
      toast.info(`Assign all 16 words before submitting (${unassignedCount(assignment)} left).`);
      return;
    }
    if (!confirmArmed) {
      setConfirmArmed(true);
      return;
    }
    setSubmitting(true);
    try {
      const response = await submitConnections(game.game_id, submissionGroups(assignment));
      setGame((previous) =>
        previous ? { ...previous, words: [], submitted: true } : previous,
      );
      setAssignment(null);
      toast.success(response.message);
    } catch (error) {
      toast.error(describeApiError(error, 'Could not submit the puzzle.'));
      setConfirmArmed(false);
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return (
      <div>
        <GamesPageHeader eyebrow="Word play" title="Connections" />
        <div className="flex justify-center py-16 text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin" />
        </div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div>
        <GamesPageHeader eyebrow="Word play" title="Connections" />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-10 text-center">
            <p className="text-sm text-muted-foreground">{loadError}</p>
            <NavLink to="/games">
              <Button variant="outline">Back to Games</Button>
            </NavLink>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!game) {
    return (
      <div>
        <GamesPageHeader eyebrow="Word play" title="Connections" />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-10 text-center">
            <p className="text-sm text-muted-foreground">Could not load today&apos;s puzzle.</p>
            <NavLink to="/games">
              <Button variant="outline">Back to Games</Button>
            </NavLink>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (game.submitted) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Word play"
          title="Connections"
          description="Find the four hidden groups of four."
        />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
            <CheckCircle2 className="h-10 w-10 text-romantic" />
            <p className="text-lg font-semibold">Submission received</p>
            <p className="max-w-sm text-sm text-muted-foreground">
              Your Connections puzzle has been submitted successfully. You can
              view the leaderboard separately.
            </p>
            <NavLink to="/games">
              <Button variant="outline">Back to Games</Button>
            </NavLink>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (game.session_status === 'pending' || game.game_id === null) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Word play"
          title="Connections"
          description="Find the four hidden groups of four."
        />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
            <p className="text-lg font-semibold">Today&apos;s puzzle is not available yet.</p>
            <p className="max-w-sm text-sm text-muted-foreground">
              {game.message ??
                'The session is waiting for its puzzle. Check back soon -- or look at what else the Games section has to offer.'}
            </p>
            <NavLink to="/games">
              <Button variant="outline">Back to Games</Button>
            </NavLink>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!assignment) {
    return (
      <div>
        <GamesPageHeader eyebrow="Word play" title="Connections" />
        <div className="flex justify-center py-16 text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin" />
        </div>
      </div>
    );
  }

  const remaining = unassignedCount(assignment);
  const complete = isComplete(assignment);

  return (
    <div>
      <GamesPageHeader
        eyebrow="Word play"
        title="Connections"
        description="Drag all 16 words into four groups of four. The groups have no names -- find the links yourself."
      />

      <div className="flex flex-col items-center gap-6">
        <p className="text-sm text-muted-foreground" data-testid="remaining-label">
          {remaining === 0
            ? 'All words assigned -- review your groups, then submit.'
            : `${remaining} ${remaining === 1 ? 'word' : 'words'} left to assign`}
        </p>

        {/* Available words */}
        <section
          aria-label="Available words"
          onDragOver={(event) => {
            event.preventDefault();
            setDropTarget(-1);
          }}
          onDragLeave={() => setDropTarget((target) => (target === -1 ? null : target))}
          onDrop={(event) => onDropOn(event, -1)}
          onClick={() => onDestinationClick(-1)}
          className={cn(
            'flex w-full max-w-2xl flex-wrap justify-center gap-2 rounded-2xl border-2 border-dashed p-4 transition-colors',
            dropTarget === -1 ? 'border-romantic bg-romantic/5' : 'border-border/70',
          )}
        >
          {assignment.available.length === 0 ? (
            <p className="py-2 text-sm text-muted-foreground">
              Every word is assigned. Drag one back here to rearrange.
            </p>
          ) : (
            assignment.available.map((word) => (
              <TileButton
                key={word}
                word={word}
                selected={selectedWord === word}
                dragging={dragWord === word}
                onClick={() => onTileClick(word)}
                onDragStart={(event) => onDragStart(event, word)}
                onDragEnd={onDragEnd}
              />
            ))
          )}
        </section>

        {/* Four destination groups */}
        <div className="grid w-full max-w-2xl gap-3 sm:grid-cols-2">
          {assignment.groups.map((group, index) => (
            <section
              key={GROUP_LABELS[index]}
              aria-label={GROUP_LABELS[index]}
              onDragOver={(event) => {
                event.preventDefault();
                setDropTarget(index);
              }}
              onDragLeave={() =>
                setDropTarget((target) => (target === index ? null : target))
              }
              onDrop={(event) => onDropOn(event, index)}
              onClick={() => onDestinationClick(index)}
              className={cn(
                'flex min-h-32 flex-col gap-2 rounded-2xl border-2 p-3 transition-colors',
                dropTarget === index
                  ? 'border-romantic bg-romantic/5'
                  : 'border-border/70 bg-card/60',
                group.length >= 4 && 'border-solid',
              )}
            >
              <header className="flex items-center justify-between">
                <h3 className="text-xs font-bold uppercase tracking-wider text-muted-foreground">
                  {GROUP_LABELS[index]}
                </h3>
                <span
                  className="text-xs tabular-nums text-muted-foreground"
                  data-testid={`group-count-${index}`}
                >
                  {group.length}/4
                </span>
              </header>
              <div className="flex flex-wrap gap-2">
                {group.map((word) => (
                  <TileButton
                    key={word}
                    word={word}
                    selected={selectedWord === word}
                    dragging={dragWord === word}
                    onClick={() => onTileClick(word)}
                    onDragStart={(event) => onDragStart(event, word)}
                    onDragEnd={onDragEnd}
                  />
                ))}
                {group.length === 0 && (
                  <p className="py-1 text-xs text-muted-foreground">
                    Drop words here -- or tap a word, then tap here.
                  </p>
                )}
              </div>
            </section>
          ))}
        </div>

        <p className="max-w-md text-center text-xs text-muted-foreground">
          Drag a tile into a group -- or tap a tile, then tap its destination.
          A full group keeps four: the earliest-placed word returns to the
          available area.
        </p>

        <div className="flex flex-wrap items-center justify-center gap-3">
          <Button
            type="button"
            onClick={submitFinal}
            disabled={!complete || submitting}
          >
            {submitting
              ? 'Submitting...'
              : confirmArmed
                ? 'Click again to confirm submission'
                : 'SUBMIT'}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={resetBoard}
            disabled={submitting}
          >
            <RotateCcw />
            Reset board
          </Button>
        </div>
      </div>
    </div>
  );
};

const TileButton = ({
  word,
  selected,
  dragging,
  onClick,
  onDragStart,
  onDragEnd,
}: {
  word: string;
  selected: boolean;
  dragging: boolean;
  onClick: () => void;
  onDragStart: (event: React.DragEvent) => void;
  onDragEnd: () => void;
}) => (
  <button
    type="button"
    draggable
    aria-label={`Word ${word}`}
    aria-pressed={selected}
    onClick={onClick}
    onDragStart={onDragStart}
    onDragEnd={onDragEnd}
    className={cn(
      'cursor-grab rounded-md border-2 border-border bg-secondary/40 px-3 py-2 text-sm font-bold uppercase transition-colors active:cursor-grabbing',
      'hover:border-romantic/60',
      selected && 'border-romantic ring-2 ring-romantic/60',
      dragging && 'border-romantic opacity-60 shadow-lg',
    )}
  >
    {word}
  </button>
);

export default ConnectionsGamePage;
