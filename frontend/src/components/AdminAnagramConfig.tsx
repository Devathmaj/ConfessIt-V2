import { useCallback, useEffect, useState } from 'react';
import { Dices, Shuffle, X } from 'lucide-react';
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
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { describeApiError } from '@/lib/apiError';
import {
  createAnagramGame,
  drawAnagramAnswer,
  getAdminAnagramGame,
  getAdminAnagramGames,
  type AnagramDraw,
  type AnagramGame,
  type AnagramGamePayload,
} from '@/services/api';

/** Today as the server numbers it, in UTC, which is also what the date field starts on. */
const todayIso = () => new Date().toISOString().slice(0, 10);

type Mode = AnagramGamePayload['mode'];

/**
 * Publishing one day's Anagram word.
 *
 * Two ways in, both ending in the same save: type a word, or let the server draw one. The
 * draw is a two-step on purpose -- `preview` picks the word and signs it to the day, the
 * screen shows that word, and only then does `POST` store it -- so the answer is visible
 * before anything is written, the admin can draw again as often as they like, and the thing
 * saved is the thing that was shown. What travels back is a token this screen cannot forge,
 * which is why `mode="random"` never offers a free text field: there would be no signature
 * behind it.
 *
 * The list below is the whole record, answers included. This is the screen that decides what
 * the player-facing puzzle becomes, so hiding the answer here would hide it from exactly the
 * person who set it. The server refuses every caller who may not publish, which is what keeps
 * the same answers off every other screen.
 *
 * One day holds one word, so a day that is already configured comes back from the server as
 * a 409 naming the date. That is shown inline rather than as a toast: a toast disappears and
 * the field is still full of a word that cannot be saved, whereas the reason to stop is worth
 * staying on screen until the operator changes the day or clears the form.
 */
export const AdminAnagramConfig = () => {
  const { logActivity } = useGamesConfig();

  const [games, setGames] = useState<AnagramGame[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [mode, setMode] = useState<Mode>('custom');
  const [gameDate, setGameDate] = useState<string>(todayIso);
  const [word, setWord] = useState('');
  const [drawn, setDrawn] = useState<AnagramDraw | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // The puzzle the last save created, so the word the server stored -- the one a draw
  // chose, or the typed word after normalisation -- is on screen the moment it exists.
  const [lastCreated, setLastCreated] = useState<AnagramGame | null>(null);

  const [detail, setDetail] = useState<AnagramGame | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setGames(await getAdminAnagramGames());
      setListError(null);
    } catch (error) {
      setListError(describeApiError(error, 'Could not load the configured puzzles.'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleDraw = async () => {
    setDrawing(true);
    setFormError(null);
    try {
      setDrawn(await drawAnagramAnswer(gameDate));
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
        mode === 'custom'
          ? { mode: 'custom', word: word.trim(), game_date: gameDate }
          : { mode: 'random', word: drawn?.word, token: drawn?.token, game_date: gameDate };
      const created = await createAnagramGame(payload);

      setLastCreated(created);
      toast.success(`Anagram configured for ${created.game_date}.`);
      logActivity({
        kind: 'puzzle',
        gameId: 'anagram',
        message: `Anagram word for ${created.game_date} set`,
      });
      setWord('');
      setDrawn(null);
      setDetail(null);
      setDetailError(null);
      await refresh();
    } catch (error) {
      setFormError(describeApiError(error, 'Could not save the puzzle.'));
    } finally {
      setSaving(false);
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

  // A day, a word of any length, and -- for a draw -- a draw to save. There is no minimum
  // length here because the server does not hold one either: any letters are a legal custom
  // answer, and the five-letter floor belongs to the draw alone.
  const hasDate = gameDate.trim().length > 0;
  const readyToSave =
    hasDate && (mode === 'custom' ? word.trim().length > 0 : drawn !== null);

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
              One word a day. It stays here until the puzzle goes live, so what you see is
              what the day will run on.
            </CardDescription>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-2">
              <Label htmlFor="anagram-date">Day</Label>
              <Input
                id="anagram-date"
                type="date"
                value={gameDate}
                onChange={(event) => {
                  setGameDate(event.target.value);
                  // A draw is signed to one day. Changing the day invalidates it, so the
                  // screen asks for a new one rather than saving a word for the old date.
                  setDrawn(null);
                  setFormError(null);
                }}
              />
            </div>

            <div className="flex gap-2" role="group" aria-label="Where the word comes from">
              <Button
                type="button"
                variant={mode === 'custom' ? 'default' : 'outline'}
                size="sm"
                aria-pressed={mode === 'custom'}
                onClick={() => {
                  setMode('custom');
                  setDrawn(null);
                  setFormError(null);
                }}
              >
                Type a word
              </Button>
              <Button
                type="button"
                variant={mode === 'random' ? 'default' : 'outline'}
                size="sm"
                aria-pressed={mode === 'random'}
                onClick={() => {
                  setMode('random');
                  setWord('');
                  setFormError(null);
                }}
              >
                <Dices />
                Draw for me
              </Button>
            </div>
          </div>

          {mode === 'custom' ? (
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
                  Word for {gameDate}
                </p>
                <p className="break-all font-mono text-2xl font-semibold uppercase tracking-widest">
                  {drawn.word}
                </p>
                <p className="text-xs text-muted-foreground">
                  Drawn by the server. Saving stores this word and no other.
                </p>
              </div>
              <Button type="button" size="sm" onClick={handleDraw} disabled={drawing}>
                <Dices />
                Draw another
              </Button>
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-3">
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={handleDraw}
                disabled={drawing || !hasDate}
              >
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
              {saving ? 'Saving...' : 'Save puzzle'}
            </Button>
            <p className="text-xs text-muted-foreground">
              {mode === 'custom'
                ? 'The word is checked for letters on save.'
                : drawn
                  ? `Saves ${drawn.word} for ${gameDate}.`
                  : 'Draw a word first.'}
            </p>
          </div>

          {formError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {formError}
            </p>
          )}

          {lastCreated && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border/60 bg-muted/40 p-4">
              <div className="min-w-0">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  {lastCreated.mode === 'random'
                    ? 'The server drew'
                    : 'Saved as'}{' '}
                  for {lastCreated.game_date}
                </p>
                <p className="break-all font-mono text-2xl font-semibold uppercase tracking-widest">
                  {lastCreated.answer}
                </p>
                <p className="text-xs text-muted-foreground">
                  Stored in uppercase, exactly as shown.
                </p>
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setLastCreated(null)}
                aria-label="Dismiss"
              >
                <X />
              </Button>
            </div>
          )}
        </div>

        <div className="flex flex-col gap-3 border-t border-border/40 pt-4">
          <div className="flex items-baseline justify-between gap-3">
            <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              Configured puzzles
            </h4>
            <span className="text-xs text-muted-foreground">
              {loading ? 'Loading...' : `${games.length} configured`}
            </span>
          </div>

          {listError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {listError}
            </p>
          )}

          {!loading && !listError && games.length === 0 && (
            <p className="text-sm text-muted-foreground">
              Nothing configured yet. Set the word above and it appears here.
            </p>
          )}

          {games.length > 0 && (
            <ul className="flex flex-col gap-2">
              {games.map((game) => (
                <li key={game.id}>
                  <div className="flex items-center justify-between gap-3 rounded-xl border border-border/60 p-3">
                    <button
                      type="button"
                      onClick={() => void openDetail(game.id)}
                      aria-label={`Inspect the puzzle for ${game.game_date}`}
                      className="flex min-w-0 flex-1 flex-wrap items-center gap-3 text-left"
                    >
                      <span className="font-mono text-sm font-semibold">{game.game_date}</span>
                      <Badge variant={game.mode === 'custom' ? 'secondary' : 'default'}>
                        {game.mode === 'custom' ? 'Typed' : 'Drawn'}
                      </Badge>
                      <span className="break-all font-mono uppercase tracking-widest">
                        {game.answer}
                      </span>
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {detail && (
            <div className="rounded-xl border border-border/60 bg-muted/40 p-4 text-sm">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 space-y-1">
                  <p className="font-semibold">Puzzle for {detail.game_date}</p>
                  <p className="break-all font-mono text-xs text-muted-foreground">
                    {detail.id}
                  </p>
                  <p>
                    {detail.mode === 'custom' ? 'Typed' : 'Drawn'} word:{' '}
                    <span className="break-all font-mono uppercase tracking-widest">
                      {detail.answer}
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

          {detailError && (
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
