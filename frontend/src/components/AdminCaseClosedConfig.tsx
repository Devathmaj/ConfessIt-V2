import { useCallback, useEffect, useState } from 'react';
import { Fingerprint, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { AdminGameStatusControls } from '@/components/AdminGameStatusControls';
import { describeApiError } from '@/lib/apiError';
import {
  approveCaseClosedBlueprint,
  createCaseClosedBlueprint,
  generateCaseClosedBlueprint,
  generateCaseClosedPackage,
  getCaseClosedBlueprints,
  getCaseClosedPackages,
  publishCaseClosedPackage,
  type CaseClosedBlueprint,
  type CaseClosedPackage,
} from '@/services/api';

export const AdminCaseClosedConfig = () => {
  const [difficulty, setDifficulty] = useState('medium');
  const [busy, setBusy] = useState(false);
  const [blueprints, setBlueprints] = useState<CaseClosedBlueprint[]>([]);
  const [packages, setPackages] = useState<CaseClosedPackage[]>([]);
  const [selectedBlueprint, setSelectedBlueprint] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setBlueprints(await getCaseClosedBlueprints());
      setPackages(await getCaseClosedPackages());
    } catch (error) {
      toast.error(describeApiError(error, 'Could not load Case Closed drafts.'));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    try {
      await fn();
      toast.success(ok);
      await refresh();
    } catch (error) {
      toast.error(describeApiError(error, 'Operation failed.'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><Fingerprint className="h-5 w-5" /> Case Closed generation</CardTitle>
        <CardDescription>Two-stage AI authoring (blueprint → package) via the shared Plot Twist LLM connection. Invalid packages never publish; published versions are immutable.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <AdminGameStatusControls gameId="case-closed" gameName="Case Closed" />
        <div className="flex items-center gap-2">
          <Select value={difficulty} onValueChange={setDifficulty}>
            <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="easy">easy</SelectItem>
              <SelectItem value="medium">medium</SelectItem>
              <SelectItem value="hard">hard</SelectItem>
            </SelectContent>
          </Select>
          <Button disabled={busy} onClick={() => void run(async () => {
            const created = await createCaseClosedBlueprint({ difficulty });
            setSelectedBlueprint(created.id);
          }, 'Draft created.')}>{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : 'New draft'}</Button>
          <Button variant="ghost" size="sm" onClick={() => void refresh()}>Refresh</Button>
        </div>

        <div className="flex flex-col gap-2">
          <h4 className="text-sm font-semibold">Blueprints ({blueprints.length})</h4>
          {blueprints.map((b) => (
            <div key={b.id} className="rounded-xl border p-3 text-sm">
              <div className="flex items-center justify-between gap-2">
                <div className="font-medium">{b.title} <Badge variant="outline">{b.status}</Badge> <Badge variant="secondary">{b.difficulty}</Badge></div>
                <div className="flex gap-1">
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => generateCaseClosedBlueprint(b.id), 'Blueprint generated.')}>Generate</Button>
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => approveCaseClosedBlueprint(b.id), 'Blueprint approved.')}>Approve</Button>
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(async () => { await generateCaseClosedPackage(b.id); }, 'Package generated.')}>Package</Button>
                </div>
              </div>
              {b.validation_errors.length > 0 && (
                <ul className="mt-2 list-disc pl-5 text-xs text-destructive">
                  {b.validation_errors.slice(0, 8).map((e, i) => <li key={i}>[{e.code}] {e.message}</li>)}
                </ul>
              )}
            </div>
          ))}
        </div>

        <div className="flex flex-col gap-2">
          <h4 className="text-sm font-semibold">Packages ({packages.length})</h4>
          {packages.map((p) => (
            <div key={p.id} className="rounded-xl border p-3 text-sm">
              <div className="flex items-center justify-between gap-2">
                <div className="font-medium">{p.title} v{p.version} <Badge variant="outline">{p.status}</Badge></div>
                <Button size="sm" disabled={busy || p.status === 'published'} onClick={() => void run(() => publishCaseClosedPackage(p.id), 'Package published.')}>Publish</Button>
              </div>
              {p.validation_errors.length > 0 && (
                <ul className="mt-2 list-disc pl-5 text-xs text-destructive">
                  {p.validation_errors.slice(0, 8).map((e, i) => <li key={i}>[{e.code}] {e.message}</li>)}
                </ul>
              )}
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
};

export default AdminCaseClosedConfig;
