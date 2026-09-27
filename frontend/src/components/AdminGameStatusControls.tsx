import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { describeApiError } from '@/lib/apiError';
import {
  getGameConfigs,
  updateGameConfig,
  type GameConfig,
} from '@/services/api';

const CADENCE_LABELS: Record<GameConfig['reset_cadence'], string> = {
  daily: 'Daily',
  every_2_days: 'Every 2 Days',
  every_3_days: 'Every 3 Days',
  every_5_days: 'Every 5 Days',
  weekly: 'Weekly (Mon–Sun UTC)',
};

/**
 * One game's availability and reset cadence, living inside that game's own
 * card rather than in a separate section.
 *
 * Enabled/disabled and visible/hidden are independent: a disabled game stays
 * in the selection list marked temporarily unavailable while its sessions
 * keep rolling; a hidden game leaves the list, refuses direct play, and stops
 * the scheduler from opening new sessions. The reset cadence belongs to the
 * game, never to the spotlight -- featuring a game changes none of this.
 *
 * The cadence picker stages its change: picking a frequency only arms a Save
 * button next to it, and nothing is sent until Save is clicked, so browsing
 * the options can never rewrite production config by itself.
 *
 * `hideResetCadence` is for games that release individually rather than on
 * any cadence (Trivia Drop): the stored cadence is never read for them, so
 * the picker is not offered and cannot suggest otherwise.
 */
export const AdminGameStatusControls = ({
  gameId,
  gameName,
  hideResetCadence = false,
}: {
  gameId: string;
  gameName: string;
  hideResetCadence?: boolean;
}) => {
  const [config, setConfig] = useState<GameConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // The staged cadence. Picking a frequency stages it without saving --
  // staging the saved value clears again -- and Save persists it.
  const [stagedCadence, setStagedCadence] = useState<GameConfig['reset_cadence'] | null>(null);
  const [cadenceError, setCadenceError] = useState<string | null>(null);
  const hasUnsavedCadence =
    stagedCadence !== null && config !== null && stagedCadence !== config.reset_cadence;

  const refresh = useCallback(async () => {
    try {
      const configs = (await getGameConfigs()) ?? [];
      setConfig(configs.find((entry) => entry.game_id === gameId) ?? null);
      setStagedCadence(null);
      setCadenceError(null);
      setLoadError(null);
    } catch (error) {
      setLoadError(describeApiError(error, 'Could not load game status.'));
    } finally {
      setLoading(false);
    }
  }, [gameId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const patch = async (payload: Partial<GameConfig>) => {
    setSaving(true);
    try {
      setConfig(await updateGameConfig(gameId, payload));
    } catch (error) {
      toast.error(describeApiError(error, 'Could not save game status.'));
    } finally {
      setSaving(false);
    }
  };

  /** Stage a cadence without saving it; staging the saved value clears. */
  const selectCadence = (next: GameConfig['reset_cadence']) => {
    setStagedCadence(config !== null && next === config.reset_cadence ? null : next);
    setCadenceError(null);
  };

  const handleSaveCadence = async () => {
    if (stagedCadence === null) return;
    setSaving(true);
    setCadenceError(null);
    try {
      setConfig(await updateGameConfig(gameId, { reset_cadence: stagedCadence }));
      setStagedCadence(null);
    } catch (error) {
      setCadenceError(describeApiError(error, 'Could not save the reset frequency.'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      role="group"
      aria-label={`${gameName} status and reset`}
      className="space-y-3 rounded-xl border border-border/60 bg-muted/40 p-3"
    >
      {loadError && (
        <p role="alert" className="text-sm font-medium text-destructive">
          {loadError}
        </p>
      )}
      {loading && !loadError && <p className="text-sm text-muted-foreground">Loading status…</p>}
      {!loading && !loadError && config === null && (
        <p className="text-sm text-muted-foreground">Status unavailable.</p>
      )}
      {!loading && !loadError && config !== null && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm font-semibold">Game status</p>
            <div className="flex gap-2">
              <Badge variant={config.enabled ? 'default' : 'secondary'}>
                {config.enabled ? 'Enabled' : 'Disabled'}
              </Badge>
              <Badge variant={config.is_hidden ? 'destructive' : 'outline'}>
                {config.is_hidden ? 'Hidden' : 'Visible'}
              </Badge>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
            <label className="flex items-center gap-2 text-sm">
              <Switch
                checked={config.enabled}
                disabled={saving}
                onCheckedChange={(next) => void patch({ enabled: next })}
                aria-label={`${gameName} enabled`}
              />
              Enabled
            </label>
            <label className="flex items-center gap-2 text-sm">
              <Switch
                checked={config.is_hidden}
                disabled={saving}
                onCheckedChange={(next) => void patch({ is_hidden: next })}
                aria-label={`${gameName} hidden`}
              />
              Hidden
            </label>
          </div>
          {!hideResetCadence && (
            <div className="flex flex-col gap-2">
              <Label>Session reset</Label>
              <div className="flex flex-wrap items-center gap-2">
                <Select
                  value={stagedCadence ?? config.reset_cadence}
                  disabled={saving}
                  onValueChange={(next) =>
                    selectCadence(next as GameConfig['reset_cadence'])
                  }
                >
                  <SelectTrigger className="w-52">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {Object.entries(CADENCE_LABELS).map(([value, label]) => (
                      <SelectItem key={value} value={value}>
                        {label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {hasUnsavedCadence && (
                  <>
                    <Button
                      type="button"
                      size="sm"
                      onClick={handleSaveCadence}
                      disabled={saving}
                    >
                      {saving ? 'Saving...' : 'Save'}
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      disabled={saving}
                      onClick={() => {
                        setStagedCadence(null);
                        setCadenceError(null);
                      }}
                    >
                      Discard
                    </Button>
                  </>
                )}
              </div>
              {hasUnsavedCadence && (
                <p role="status" className="text-xs text-muted-foreground">
                  Unsaved changes — the new frequency applies from the next
                  session onwards once saved. The current session is untouched.
                </p>
              )}
              {cadenceError && (
                <p role="alert" className="text-sm font-medium text-destructive">
                  {cadenceError}
                </p>
              )}
            </div>
          )}
          <p className="text-xs text-muted-foreground">
            {config.is_hidden
              ? 'Hidden: absent from selection, direct play refused, no new sessions.'
              : config.enabled
                ? 'Normal: playable while its session is configured.'
                : 'Disabled: shown as temporarily unavailable, direct play refused, sessions keep rolling.'}
          </p>
        </>
      )}
    </div>
  );
};

export default AdminGameStatusControls;
