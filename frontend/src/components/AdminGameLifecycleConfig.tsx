import { useCallback, useEffect, useState } from 'react';
import { RefreshCw, Settings2 } from 'lucide-react';
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
  runScheduledResets,
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

const GAME_LABELS: Record<string, string> = {
  wordle: 'Wordle',
  anagram: 'Anagram',
  'couple-questionnaire': 'Couple Questionnaire',
  'reaction-time': 'Reaction Time',
};

/**
 * Per-game availability and reset cadence, owned by the server.
 *
 * Enabled/disabled and visible/hidden are independent: a disabled game stays
 * in the selection list marked temporarily unavailable while its sessions
 * keep rolling; a hidden game leaves the list, refuses direct play, and stops
 * the scheduler from opening new sessions. The reset cadence belongs to the
 * game, never to the spotlight -- featuring a game changes none of this.
 */
export const AdminGameLifecycleConfig = () => {
  const [configs, setConfigs] = useState<GameConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [resetSummary, setResetSummary] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setConfigs(await getGameConfigs());
      setLoadError(null);
    } catch (error) {
      setLoadError(describeApiError(error, 'Could not load game configuration.'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const patch = async (gameId: string, payload: Partial<GameConfig>) => {
    try {
      const updated = await updateGameConfig(gameId, payload);
      setConfigs((prev) => prev.map((entry) => (entry.game_id === gameId ? updated : entry)));
    } catch (error) {
      toast.error(describeApiError(error, 'Could not save game configuration.'));
    }
  };

  const handleRunResets = async () => {
    setResetting(true);
    setResetSummary(null);
    try {
      const result = await runScheduledResets();
      const parts = Object.entries(result.resets).map(
        ([gameId, info]) => `${GAME_LABELS[gameId] ?? gameId}: ${info.action}`,
      );
      setResetSummary(parts.join(' · '));
      toast.success('Session boundaries evaluated.');
    } catch (error) {
      toast.error(describeApiError(error, 'Could not run the session resets.'));
    } finally {
      setResetting(false);
    }
  };

  return (
    <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-xl">
              <Settings2 className="h-5 w-5" />
              Game status &amp; session resets
            </CardTitle>
            <CardDescription>
              Availability and the per-game reset cadence. Sessions roll at 00:01 UTC under
              each game&apos;s own cadence, whether or not the game is featured.
            </CardDescription>
          </div>
          <Button type="button" variant="outline" size="sm" onClick={handleRunResets} disabled={resetting}>
            <RefreshCw />
            {resetting ? 'Evaluating...' : 'Run resets now'}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {loadError && (
          <p role="alert" className="text-sm font-medium text-destructive">
            {loadError}
          </p>
        )}
        {loading && <p className="text-sm text-muted-foreground">Loading...</p>}
        {!loading &&
          !loadError &&
          configs.map((config) => (
            <div
              key={config.game_id}
              className="space-y-3 rounded-xl border border-border/60 p-4"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="font-semibold">{GAME_LABELS[config.game_id] ?? config.game_id}</p>
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
                    onCheckedChange={(next) => void patch(config.game_id, { enabled: next })}
                    aria-label={`${config.game_id} enabled`}
                  />
                  Enabled
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <Switch
                    checked={config.is_hidden}
                    onCheckedChange={(next) => void patch(config.game_id, { is_hidden: next })}
                    aria-label={`${config.game_id} hidden`}
                  />
                  Hidden
                </label>
              </div>
              <div className="flex flex-wrap items-end gap-3">
                <div className="flex flex-col gap-2">
                  <Label>Session reset</Label>
                  <Select
                    value={config.reset_cadence}
                    onValueChange={(next) =>
                      void patch(config.game_id, {
                        reset_cadence: next as GameConfig['reset_cadence'],
                      })
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
                </div>
              </div>
              {(config.game_id === 'wordle' || config.game_id === 'anagram') && (
                <p className="text-xs text-muted-foreground">
                  Content mode (random / manual) is staged and saved from the{' '}
                  {GAME_LABELS[config.game_id] ?? config.game_id} card below, so a stray
                  click can never flip it without an explicit Save.
                </p>
              )}
              <p className="text-xs text-muted-foreground">
                {config.is_hidden
                  ? 'Hidden: absent from selection, direct play refused, no new sessions.'
                  : config.enabled
                    ? 'Normal: playable while its session is configured.'
                    : 'Disabled: shown as temporarily unavailable, direct play refused, sessions keep rolling.'}
              </p>
            </div>
          ))}
        {resetSummary && (
          <p className="text-xs text-muted-foreground">Last run — {resetSummary}</p>
        )}
      </CardContent>
    </Card>
  );
};

export default AdminGameLifecycleConfig;
