import { useCallback, useEffect, useMemo, useState } from 'react';
import { BookOpenText, FlaskConical, HeartHandshake, Loader2 } from 'lucide-react';
import { toast } from 'sonner';

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { AdminGameStatusControls } from '@/components/AdminGameStatusControls';
import { describeApiError } from '@/lib/apiError';
import {
  approvePlotTwistBlueprint,
  createPlotTwistBlueprint,
  generatePlotTwistBlueprint,
  generatePlotTwistPackage,
  getCurrentAdminPlotTwistSession,
  getPlotTwistBlueprint,
  getPlotTwistBlueprints,
  getPlotTwistPackage,
  getPlotTwistPackages,
  publishPlotTwistPackage,
  regeneratePlotTwistScene,
  simulatePlotTwistPackage,
  updatePlotTwistPackage,
  type PlotTwistBlueprint,
  type PlotTwistPackage,
  type PlotTwistSimulation,
} from '@/services/api';
import { cn } from '@/lib/utils';

const DIFFICULTIES = ['easy', 'medium', 'hard'] as const;

const STATUS_LABELS: Record<string, string> = {
  draft: 'Draft',
  generating: 'Generating…',
  validation_failed: 'Validation failed',
  ready_for_review: 'Ready for review',
  published: 'Published',
  generation_failed: 'Generation failed',
  approved: 'Approved',
};

const asRecord = (value: unknown): Record<string, any> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, any>)
    : {};

const asList = (value: unknown): Array<any> => (Array.isArray(value) ? value : []);

/**
 * The Plot Twist generation console.
 *
 * Availability and the reset cadence live in the shared status controls
 * (the same switches every game owns); everything else here is the
 * AI story pipeline: difficulty-only generation parameters, blueprint
 * approval, full package generation, per-scene inspection and
 * regeneration, validation review, sample-path simulation, manual edits
 * and publication. Nothing publishes unless validation has succeeded and
 * an administrator approves it; published versions are immutable and
 * later work creates new versions.
 */
export const AdminPlotTwistConfig = () => {
  const [difficulty, setDifficulty] = useState<string>('medium');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const [blueprints, setBlueprints] = useState<PlotTwistBlueprint[]>([]);
  const [packages, setPackages] = useState<PlotTwistPackage[]>([]);
  const [selectedBlueprintId, setSelectedBlueprintId] = useState<string | null>(null);
  const [selectedPackageId, setSelectedPackageId] = useState<string | null>(null);

  const [session, setSession] = useState<{
    id: string;
    period_start_date: string | null;
    period_end_date: string | null;
    story_package_id: string | null;
  } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [blueprintList, packageList] = await Promise.all([
        getPlotTwistBlueprints(),
        getPlotTwistPackages(),
      ]);
      setBlueprints(blueprintList);
      setPackages(packageList);
      setSelectedBlueprintId((current) => current ?? blueprintList[0]?.id ?? null);
      setSelectedPackageId((current) => current ?? packageList[0]?.id ?? null);
    } catch (error) {
      toast.error(describeApiError(error, 'Could not load Plot Twist stories.'));
    }
    try {
      setSession(await getCurrentAdminPlotTwistSession());
    } catch {
      setSession(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const selectedBlueprint = useMemo(
    () => blueprints.find((entry) => entry.id === selectedBlueprintId) ?? null,
    [blueprints, selectedBlueprintId],
  );
  const selectedPackage = useMemo(
    () => packages.find((entry) => entry.id === selectedPackageId) ?? null,
    [packages, selectedPackageId],
  );

  const run = useCallback(
    async (label: string, action: () => Promise<unknown>) => {
      setBusy(true);
      setNotice(null);
      try {
        await action();
        await refresh();
      } catch (error) {
        setNotice(describeApiError(error, `${label} failed.`));
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  const handleNewBlueprint = () =>
    run('Starting generation', async () => {
      const created = await createPlotTwistBlueprint({ difficulty });
      const generated = await generatePlotTwistBlueprint(created.id);
      setSelectedBlueprintId(generated.id);
      toast.success('Blueprint generated — review it below.');
    });

  const handleRegenerateBlueprint = () => {
    if (!selectedBlueprint) return;
    void run('Regenerating the blueprint', () =>
      generatePlotTwistBlueprint(selectedBlueprint.id).then((row) => {
        setSelectedBlueprintId(row.id);
      }),
    );
  };

  const handleApproveBlueprint = () => {
    if (!selectedBlueprint) return;
    void run('Approving the blueprint', () =>
      approvePlotTwistBlueprint(selectedBlueprint.id),
    );
  };

  const handleGeneratePackage = () => {
    if (!selectedBlueprint) return;
    void run('Generating the story package', async () => {
      const created = await generatePlotTwistPackage(selectedBlueprint.id);
      setSelectedPackageId(created.id);
      toast.success('Story package generated — inspect and validate it below.');
    });
  };

  const handlePublish = () => {
    if (!selectedPackage) return;
    void run('Publishing', () => publishPlotTwistPackage(selectedPackage.id));
  };

  return (
    <Card className="[border-radius:var(--edge-card)] border-border/60 bg-card/70 backdrop-blur">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-xl">
              <HeartHandshake className="h-5 w-5" />
              Plot Twist
            </CardTitle>
            <CardDescription>
              An asynchronous story for couples, generated before play and executed
              deterministically. Availability and the reset cadence are the only
              event knobs — story content is versioned below.
            </CardDescription>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        <AdminGameStatusControls gameId="plot-twist" gameName="Plot Twist" />

        <div className="flex flex-col gap-3 border-t border-border/40 pt-4">
          <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Current session
          </h4>
          {!session ? (
            <p className="text-sm text-muted-foreground">
              No active session right now. The reset loop opens the next period automatically
              once a story package is published.
            </p>
          ) : (
            <p className="[border-radius:var(--edge-card-sm)] border border-border/60 bg-muted/40 p-3 text-sm">
              Current session period:{' '}
              <span className="font-mono font-medium">
                {session.period_start_date ?? '—'}
                {session.period_end_date ? ` → ${session.period_end_date}` : ''}
              </span>{' '}
              <span className="text-muted-foreground">
                {session.story_package_id
                  ? `playing story version ${session.story_package_id.slice(0, 8)}…`
                  : 'waiting for a published story.'}
              </span>
            </p>
          )}
        </div>

        <div className="border-t border-border/40 pt-4">
          <h4 className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            <FlaskConical className="h-4 w-4" />
            1 · Story generation (difficulty only)
          </h4>
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-2">
              <Label htmlFor="plot-twist-difficulty">Difficulty</Label>
              <Select value={difficulty} onValueChange={setDifficulty} disabled={busy}>
                <SelectTrigger id="plot-twist-difficulty" className="w-44">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {DIFFICULTIES.map((level) => (
                    <SelectItem key={level} value={level}>
                      {level[0].toUpperCase() + level.slice(1)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button type="button" disabled={busy} onClick={handleNewBlueprint}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
              Generate blueprint
            </Button>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            Generation runs the two-stage AI pipeline with bounded repair. It can take a
            minute or more; failures mark the row without touching published versions.
          </p>
        </div>

        {notice && (
          <p role="alert" className="text-sm font-medium text-destructive">
            {notice}
          </p>
        )}

        <div className="grid gap-4 lg:grid-cols-2">
          <div className="flex flex-col gap-2">
            <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              2 · Blueprints
            </h4>
            {blueprints.length === 0 && (
              <p className="text-sm text-muted-foreground">No blueprints yet.</p>
            )}
            {blueprints.map((entry) => (
              <button
                key={entry.id}
                type="button"
                onClick={() => setSelectedBlueprintId(entry.id)}
                className={cn(
                  'rounded-xl border p-3 text-left text-sm transition-colors',
                  entry.id === selectedBlueprintId
                    ? 'border-romantic/60 bg-romantic/5'
                    : 'border-border/60 hover:border-romantic/40',
                )}
              >
                <span className="flex items-center justify-between gap-2">
                  <span className="truncate font-semibold">{entry.title}</span>
                  <Badge variant={entry.status === 'approved' ? 'default' : 'outline'}>
                    {STATUS_LABELS[entry.status] ?? entry.status}
                  </Badge>
                </span>
                <span className="mt-1 block text-xs text-muted-foreground">
                  {entry.difficulty} · {entry.id.slice(0, 8)}
                </span>
              </button>
            ))}
          </div>
          <div className="flex flex-col gap-2">
            <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              3 · Story packages
            </h4>
            {packages.length === 0 && (
              <p className="text-sm text-muted-foreground">No story packages yet.</p>
            )}
            {packages.map((entry) => (
              <button
                key={entry.id}
                type="button"
                onClick={() => setSelectedPackageId(entry.id)}
                className={cn(
                  'rounded-xl border p-3 text-left text-sm transition-colors',
                  entry.id === selectedPackageId
                    ? 'border-romantic/60 bg-romantic/5'
                    : 'border-border/60 hover:border-romantic/40',
                )}
              >
                <span className="flex items-center justify-between gap-2">
                  <span className="truncate font-semibold">
                    {entry.title} <span className="text-muted-foreground">v{entry.version}</span>
                  </span>
                  <Badge variant={entry.status === 'published' ? 'default' : 'outline'}>
                    {STATUS_LABELS[entry.status] ?? entry.status}
                  </Badge>
                </span>
                <span className="mt-1 block text-xs text-muted-foreground">
                  {entry.id.slice(0, 8)} · {entry.blueprint_id.slice(0, 8)}
                </span>
              </button>
            ))}
          </div>
        </div>

        {selectedBlueprint && (
          <BlueprintDetail
            blueprint={selectedBlueprint}
            busy={busy}
            onRegenerate={handleRegenerateBlueprint}
            onApprove={handleApproveBlueprint}
            onGeneratePackage={handleGeneratePackage}
            onReload={async (id) => {
              const row = await getPlotTwistBlueprint(id);
              setBlueprints((prev) => prev.map((entry) => (entry.id === id ? row : entry)));
            }}
          />
        )}

        {selectedPackage && (
          <PackageDetail
            storyPackage={selectedPackage}
            busy={busy}
            onPublish={handlePublish}
            onReload={async (id) => {
              const row = await getPlotTwistPackage(id);
              setPackages((prev) => prev.map((entry) => (entry.id === id ? row : entry)));
            }}
            onChanged={(row) => {
              setPackages((prev) => prev.map((entry) => (entry.id === row.id ? row : entry)));
            }}
          />
        )}
      </CardContent>
    </Card>
  );
};

const BlueprintDetail = ({
  blueprint,
  busy,
  onRegenerate,
  onApprove,
  onGeneratePackage,
  onReload,
}: {
  blueprint: PlotTwistBlueprint;
  busy: boolean;
  onRegenerate: () => void;
  onApprove: () => void;
  onGeneratePackage: () => void;
  onReload: (id: string) => Promise<void>;
}) => {
  const data = asRecord(blueprint.blueprint);
  useEffect(() => {
    if (blueprint.status === 'generating') {
      const timer = window.setTimeout(() => void onReload(blueprint.id), 8000);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [blueprint.status, blueprint.id, onReload]);
  return (
    <div className="rounded-xl border border-border/60 bg-muted/30 p-4">
      <h5 className="flex items-center gap-2 text-sm font-bold">
        <BookOpenText className="h-4 w-4" />
        Blueprint: {blueprint.title}
      </h5>
      <div className="mt-2 flex flex-wrap gap-2">
        <Button type="button" size="sm" variant="outline" disabled={busy} onClick={onRegenerate}>
          Retry generation
        </Button>
        <Button
          type="button"
          size="sm"
          disabled={busy || blueprint.status !== 'ready_for_review'}
          onClick={onApprove}
        >
          Approve blueprint
        </Button>
        <Button
          type="button"
          size="sm"
          variant="secondary"
          disabled={busy || blueprint.status !== 'approved'}
          onClick={onGeneratePackage}
        >
          Generate story package
        </Button>
      </div>
      {blueprint.validation_errors.length > 0 && (
        <ValidationList errors={blueprint.validation_errors} />
      )}
      {Object.keys(data).length > 0 ? (
        <dl className="mt-3 space-y-2 text-sm">
          <DetailRow label="Premise" value={String(data.premise ?? '—')} />
          <DetailRow label="Main path" value={asList(data.main_path).join(' → ') || '—'} />
          <DetailRow
            label="State variables"
            value={asList(data.state_vars).map((v: any) => v?.name).join(', ') || '—'}
          />
          <DetailRow
            label="Endings"
            value={asList(data.endings).map((e: any) => e?.title ?? e?.id).join(' · ') || '—'}
          />
          <DetailRow label="Constraints" value={asList(data.constraints).join('; ') || '—'} />
        </dl>
      ) : (
        <p className="mt-2 text-sm text-muted-foreground">
          No generated content yet — run generation above.
        </p>
      )}
    </div>
  );
};

const PackageDetail = ({
  storyPackage,
  busy,
  onPublish,
  onReload,
  onChanged,
}: {
  storyPackage: PlotTwistPackage;
  busy: boolean;
  onPublish: () => void;
  onReload: (id: string) => Promise<void>;
  onChanged: (row: PlotTwistPackage) => void;
}) => {
  const data = asRecord(storyPackage.package);
  const scenes = asList(data.scenes);
  const endings = asList(data.endings);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [simulateInput, setSimulateInput] = useState('');
  const [simulation, setSimulation] = useState<PlotTwistSimulation | null>(null);
  const [simulating, setSimulating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [editError, setEditError] = useState<string | null>(null);

  const toggle = (id: string) => setExpanded((current) => (current === id ? null : id));

  const handleRegenerateScene = async (sceneId: string) => {
    try {
      onChanged(await regeneratePlotTwistScene(storyPackage.id, sceneId));
      toast.success('Scene regenerated.');
    } catch (error) {
      toast.error(describeApiError(error, 'Could not regenerate the scene.'));
    }
  };

  const handleSimulate = async () => {
    setSimulating(true);
    try {
      const ids = simulateInput.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean);
      setSimulation(await simulatePlotTwistPackage(storyPackage.id, ids));
    } catch (error) {
      toast.error(describeApiError(error, 'Simulation failed.'));
    } finally {
      setSimulating(false);
    }
  };

  const handleSaveEdit = async () => {
    setEditError(null);
    try {
      const parsed = JSON.parse(draft) as Record<string, unknown>;
      onChanged(await updatePlotTwistPackage(storyPackage.id, parsed));
      setEditing(false);
      toast.success('Story package updated and re-validated.');
    } catch (error) {
      setEditError(describeApiError(error, 'Could not save the edited package.'));
    }
  };

  return (
    <div className="rounded-xl border border-border/60 bg-muted/30 p-4">
      <h5 className="flex items-center gap-2 text-sm font-bold">
        <BookOpenText className="h-4 w-4" />
        Package: {storyPackage.title} v{storyPackage.version}
      </h5>
      <div className="mt-2 flex flex-wrap gap-2">
        <Button
          type="button"
          size="sm"
          disabled={busy || storyPackage.status === 'published'}
          onClick={onPublish}
        >
          Publish version
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={storyPackage.status === 'published'}
          onClick={() => {
            setDraft(JSON.stringify(data, null, 2));
            setEditing((value) => !value);
          }}
        >
          {editing ? 'Close editor' : 'Edit story JSON'}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => void onReload(storyPackage.id)}>
          Reload
        </Button>
      </div>
      {storyPackage.status === 'published' && (
        <p className="mt-2 text-xs text-muted-foreground">
          Published versions are immutable — further work creates a new version from the approved blueprint.
        </p>
      )}
      {storyPackage.validation_errors.length > 0 && (
        <ValidationList errors={storyPackage.validation_errors} />
      )}

      {editing && (
        <div className="mt-3 flex flex-col gap-2">
          <Label htmlFor="plot-twist-json">Story package JSON (validated on save)</Label>
          <textarea
            id="plot-twist-json"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            rows={14}
            spellCheck={false}
            className="rounded-xl border border-border/60 bg-background p-3 font-mono text-xs"
          />
          {editError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {editError}
            </p>
          )}
          <div>
            <Button type="button" size="sm" onClick={handleSaveEdit}>
              Save and re-validate
            </Button>
          </div>
        </div>
      )}

      <h6 className="mt-4 text-xs font-bold uppercase tracking-wide text-muted-foreground">
        Scenes ({scenes.length})
      </h6>
      <div className="mt-2 flex flex-col gap-2">
        {scenes.map((scene: any) => (
          <div key={String(scene?.id)} className="rounded-xl border border-border/50 bg-background/60 p-3">
            <button
              type="button"
              onClick={() => toggle(String(scene?.id))}
              className="flex w-full items-center justify-between gap-2 text-left text-sm"
            >
              <span className="font-semibold">
                {String(scene?.title ?? scene?.id)}{' '}
                <span className="font-normal text-muted-foreground">
                  · {String(scene?.type)} · {String(scene?.actor ?? 'either')} · slot {String(scene?.slot ?? 'core')}
                </span>
              </span>
              <Badge variant="outline">{expanded === String(scene?.id) ? '–' : '+'}</Badge>
            </button>
            {expanded === String(scene?.id) && (
              <div className="mt-2 space-y-2 text-sm">
                <p className="whitespace-pre-line text-muted-foreground">{String(scene?.narrative_text ?? '')}</p>
                <p className="text-xs">
                  Requires: {(asList(scene?.requires_tags).join(', ') || '—')} · Provides:{' '}
                  {(asList(scene?.provides_tags).join(', ') || '—')} · Forbidden:{' '}
                  {(asList(scene?.forbidden_tags).join(', ') || '—')}
                </p>
                {asList(scene?.choices).map((choice: any) => (
                  <div key={String(choice?.id)} className="rounded-lg border border-border/40 p-2">
                    <p className="font-medium">{String(choice?.text)}</p>
                    {choice?.consequence_text && (
                      <p className="text-xs text-muted-foreground">{String(choice.consequence_text)}</p>
                    )}
                    <p className="mt-1 font-mono text-xs">
                      → {String(choice?.next_scene_id)}
                      {choice?.reward_tier ? ` · reward: ${String(choice.reward_tier)}` : ''}
                    </p>
                  </div>
                ))}
                {scene?.is_terminal && (
                  <p className="text-xs font-semibold">Terminal → ending {String(scene?.ending_id ?? '')}</p>
                )}
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={storyPackage.status === 'published'}
                  onClick={() => void handleRegenerateScene(String(scene?.id))}
                >
                  Regenerate this scene
                </Button>
              </div>
            )}
          </div>
        ))}
      </div>

      <h6 className="mt-4 text-xs font-bold uppercase tracking-wide text-muted-foreground">
        Endings ({endings.length})
      </h6>
      <ul className="mt-2 flex flex-col gap-2 text-sm">
        {endings.map((ending: any) => (
          <li key={String(ending?.id)} className="rounded-xl border border-border/50 bg-background/60 p-3">
            <p className="font-semibold">
              {String(ending?.title ?? ending?.id)}{' '}
              <span className="font-normal text-muted-foreground">
                · {String(ending?.type)} · priority {String(ending?.priority ?? 0)}
                {ending?.success ? ' · success' : ''} · tier {String(ending?.reward_tier ?? '—')}
              </span>
            </p>
            <p className="mt-1 whitespace-pre-line text-muted-foreground">{String(ending?.narrative ?? '')}</p>
          </li>
        ))}
      </ul>

      <h6 className="mt-4 text-xs font-bold uppercase tracking-wide text-muted-foreground">
        Preview: simulate a decision path
      </h6>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <div className="flex min-w-52 flex-1 flex-col gap-2">
          <Label htmlFor="plot-twist-simulate">Choice ids in order (comma or space separated)</Label>
          <Input
            id="plot-twist-simulate"
            value={simulateInput}
            onChange={(event) => setSimulateInput(event.target.value)}
            placeholder="leave-auditorium, silence-alarm …"
            className="rounded-full"
          />
        </div>
        <Button type="button" size="sm" disabled={simulating} onClick={handleSimulate}>
          {simulating ? 'Simulating…' : 'Simulate'}
        </Button>
      </div>
      {simulation && (
        <div className="mt-2 rounded-xl border border-border/50 bg-background/60 p-3 text-sm">
          {simulation.valid ? (
            <>
              <p>
                Reached ending: <span className="font-semibold">{simulation.ending_title ?? simulation.ending_id ?? '—'}</span>
              </p>
              <p className="mt-1 font-mono text-xs">
                {simulation.steps.map((step: any) => `${String(step.scene_id)}:${String(step.choice_id)}`).join(' → ') || '(no steps)'}
              </p>
              {simulation.treasures.length > 0 && (
                <p className="mt-1 text-xs">Discoveries: {simulation.treasures.join(', ')}</p>
              )}
            </>
          ) : (
            <p role="alert" className="font-medium text-destructive">
              {simulation.error ?? 'That path is not valid.'}
            </p>
          )}
        </div>
      )}
    </div>
  );
};

const DetailRow = ({ label, value }: { label: string; value: string }) => (
  <div className="grid gap-1 sm:grid-cols-[10rem_1fr]">
    <dt className="text-xs font-bold uppercase tracking-wide text-muted-foreground">{label}</dt>
    <dd className="whitespace-pre-line">{value}</dd>
  </div>
);

const ValidationList = ({
  errors,
}: {
  errors: Array<{ scene_id?: string | null; code: string; message: string }>;
}) => (
  <div className="mt-3 rounded-xl border border-destructive/40 bg-destructive/5 p-3">
    <p className="text-sm font-bold text-destructive">Validation errors ({errors.length})</p>
    <ul className="mt-1 space-y-1 text-sm">
      {errors.slice(0, 12).map((error, index) => (
        <li key={`${error.code}-${index}`}>
          <span className="font-mono text-xs">[{error.code}]</span>{' '}
          {error.scene_id && <span className="font-mono text-xs">{error.scene_id}: </span>}
          {error.message}
        </li>
      ))}
      {errors.length > 12 && (
        <li className="text-xs text-muted-foreground">…and {errors.length - 12} more.</li>
      )}
    </ul>
  </div>
);

export default AdminPlotTwistConfig;
