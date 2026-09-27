import { useCallback, useEffect, useState } from 'react';
import { KeyRound } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { AdminGameStatusControls } from '@/components/AdminGameStatusControls';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { describeApiError } from '@/lib/apiError';
import {
  getGameConfigs,
  updateGameConfig,
} from '@/services/api';

const GRID_SIZES = [12, 13, 14, 15] as const;

// The lock count is never stored here: the backend owns the single
// grid-size -> locks mapping and the game state carries the derived
// number. This label only restates the identity the mapping currently is
// (a 12x12 maze carries 12 locks); if the backend ever changes it, this text
// goes stale while every number the game plays on stays correct.
const locksLabel = (size: number) => `${size} locks`;

/**
 * Two-Key Escape administration: the session/reset controls every game owns,
 * plus the one gameplay knob this game has -- the maze edge length.
 *
 * The lock count is never configured: it derives from the grid size, shown
 * here so the operator sees the consequence of the pick. Keys never run out
 * -- every use costs the cooldown and a Navigator move. The size persists
 * across session resets on the server; a new period deals a fresh maze at
 * the stored size while the configured value stays put.
 *
 * Picking a size only stages it: a Save button appears next to the picker and
 * nothing is sent until it is clicked, so browsing the options can never
 * rewrite production config by itself.
 */
export const AdminTwoKeyEscapeConfig = () => {
  const { logActivity } = useGamesConfig();

  const [serverSize, setServerSize] = useState<number>(12);
  const [stagedSize, setStagedSize] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const hasUnsavedSize = stagedSize !== null && stagedSize !== serverSize;

  const refresh = useCallback(async () => {
    try {
      const configs = await getGameConfigs();
      setServerSize(configs.find((entry) => entry.game_id === 'two-key-escape')?.grid_size ?? 12);
      setStagedSize(null);
      setSaveError(null);
    } catch {
      setServerSize(12);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  /** Stage a size without saving it; staging the saved value clears. */
  const selectSize = (next: number) => {
    setStagedSize(next === serverSize ? null : next);
    setSaveError(null);
  };

  const handleSave = async () => {
    const next = stagedSize;
    if (next === null) return;
    setSaving(true);
    setSaveError(null);
    try {
      const updated = await updateGameConfig('two-key-escape', { grid_size: next });
      setServerSize(updated.grid_size ?? next);
      setStagedSize(null);
      toast.success(`Maze size set to ${next}x${next} (${locksLabel(next)}).`);
      logActivity({
        kind: 'settings',
        gameId: 'two-key-escape',
        message: `Two-Key Escape · Grid size set to ${next}x${next}`,
      });
    } catch (error) {
      setSaveError(describeApiError(error, 'Could not save the grid size.'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-xl">
              <KeyRound className="h-5 w-5" />
              Two-Key Escape
            </CardTitle>
            <CardDescription>
              One cooperative maze per couple: a Navigator moves, a Key Operator unlocks.
              Sessions roll on this game&apos;s own reset cadence with a fresh maze each
              period at the stored size.
            </CardDescription>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        <AdminGameStatusControls gameId="two-key-escape" gameName="Two-Key Escape" />

        <div className="flex flex-col gap-3 border-t border-border/40 pt-4">
          <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Gameplay
          </h4>
          <div className="flex flex-col gap-2">
            <Label>Grid size</Label>
            <div className="flex flex-wrap items-center gap-2">
              <Select
                value={stagedSize !== null ? String(stagedSize) : String(serverSize)}
                disabled={loading || saving}
                onValueChange={(next) => selectSize(Number(next))}
              >
                <SelectTrigger className="w-52">
                  <SelectValue placeholder={loading ? 'Loading…' : 'Pick a size'} />
                </SelectTrigger>
                <SelectContent>
                  {GRID_SIZES.map((size) => (
                    <SelectItem key={size} value={String(size)}>
                      {size} × {size} · {locksLabel(size)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {hasUnsavedSize && (
                <>
                  <Button type="button" size="sm" onClick={handleSave} disabled={saving}>
                    {saving ? 'Saving...' : 'Save'}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    disabled={saving}
                    onClick={() => {
                      setStagedSize(null);
                      setSaveError(null);
                    }}
                  >
                    Discard
                  </Button>
                </>
              )}
            </div>
            {hasUnsavedSize && stagedSize !== null && (
              <p role="status" className="text-xs text-muted-foreground">
                Unsaved changes — {stagedSize}×{stagedSize} ({locksLabel(stagedSize)}) applies to
                new sessions once saved. The live maze is never rewritten.
              </p>
            )}
            {saveError && (
              <p role="alert" className="text-sm font-medium text-destructive">
                {saveError}
              </p>
            )}
            <p className="text-xs text-muted-foreground">
              The only gameplay setting. Lock count follows the size automatically; cooldown
              (10s) and scoring are fixed by the game. Applies to new
              sessions — the live maze is never rewritten.
            </p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
};

export default AdminTwoKeyEscapeConfig;
