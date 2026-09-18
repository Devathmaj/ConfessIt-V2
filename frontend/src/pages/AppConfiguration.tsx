import { AdminNavigation } from '@/components/AdminNavigation';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { useEffect, useState, useRef } from 'react';
import { cn } from '@/lib/utils';
import type { Cohort } from '@/lib/accountFields';
import { cohortDragPayload, cohortFromDragPayload, cohortKey, cohortLabel, isSameCohort } from '@/lib/accountFields';

// Base URL for backend API
const API_BASE_URL = 'http://localhost:8001';

interface Slot {
  id: string;
  cohorts: Cohort[];  // The (year, department) pairs in this slot
}

/**
 * Operator-facing matchmaking slot configuration with drag-and-drop UI.
 *
 * - Cohort palette at top: every (year, department) pair that exists in the user table
 * - Initially one slot box containing all cohorts (represents "any cohort can match with any")
 * - "+" button creates new empty slot boxes
 * - Drag cohort cards from palette into slot boxes
 * - A cohort can be in multiple slots but only once per slot
 * - Save button commits configuration to database
 */
export const AppConfiguration = () => {
  const [allCohorts, setAllCohorts] = useState<Cohort[]>([]);
  const [slots, setSlots] = useState<Slot[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [dragSource, setDragSource] = useState<'palette' | 'slot' | null>(null);
  const [draggedCohortKey, setDraggedCohortKey] = useState<string | null>(null);
  const [draggedFromSlotId, setDraggedFromSlotId] = useState<string | null>(null);
  const [dragOverSlotId, setDragOverSlotId] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<'idle' | 'success' | 'error'>('idle');
const [saveError, setSaveError] = useState<string | null>(null);

  const slotRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  // Fetch initial data
  useEffect(() => {
    const fetchData = async () => {
      try {
        setIsLoading(true);
        const response = await fetch(`${API_BASE_URL}/admin/config`, {
          headers: {
            Authorization: `Bearer ${localStorage.getItem('accessToken')}`,
          },
        });
        if (!response.ok) throw new Error('Failed to load configuration');
        const data = await response.json();

        const cohorts: Cohort[] = data.cohorts || [];
        setAllCohorts(cohorts);

        // Convert backend slots (pairs) to frontend slots (arrays of cohorts)
        const backendSlots = data.slots || [];

        if (backendSlots.length === 0 && cohorts.length > 0) {
          // No slots yet, create initial slot with all cohorts
          const initialSlot: Slot = {
            id: 'initial-' + Date.now(),
            cohorts: [...cohorts],
          };
          setSlots([initialSlot]);
        } else {
          // Reconstruct slots from pairs.
          //
          // Pairs are saved as (first cohort, other cohort), so grouping by the first cohort
          // recovers the slot each pair came from. The grouping key has to be the whole pair
          // rather than the department alone: "CSE 2nd Year" and "CSE 3rd Year" are different
          // cohorts and must not be merged into one slot by sharing a department.
          const slotMap = new Map<string, Cohort[]>();

          for (const pair of backendSlots) {
            const { year_a, department_a, year_b, department_b } = pair;
            const cohortA: Cohort = { year: year_a, department: department_a };
            const cohortB: Cohort = { year: year_b, department: department_b };
            const groupKey = cohortKey(cohortA);
            const group = slotMap.get(groupKey);
            if (!group) {
              slotMap.set(groupKey, [cohortA, cohortB]);
              continue;
            }
            // A cohort appears once per slot, so the self-pair A-A must not list it twice.
            for (const cohort of [cohortA, cohortB]) {
              if (!group.some(existing => isSameCohort(existing, cohort))) group.push(cohort);
            }
          }

          // The id is client-side only: the server assigns its own when the slot is saved, and
          // the delete loop matches on the ids it handed back. So an index is enough to keep the
          // React keys unique.
          const reconstructedSlots: Slot[] = Array.from(slotMap.values()).map((group, index) => ({
            id: `reconstructed-${index}`,
            cohorts: group,
          }));

          // If no slots reconstructed but cohorts exist, create initial slot
          if (reconstructedSlots.length === 0 && cohorts.length > 0) {
            setSlots([{
              id: 'initial-' + Date.now(),
              cohorts: [...cohorts],
            }]);
          } else {
            setSlots(reconstructedSlots);
          }
        }
      } catch (error) {
        console.error('Failed to fetch configuration:', error);
      } finally {
        setIsLoading(false);
      }
    };
    fetchData();
  }, []);

  // Create a new empty slot
  const createNewSlot = () => {
    const newSlot: Slot = {
      id: 'slot-' + Date.now() + '-' + Math.random().toString(36).substr(2, 9),
      cohorts: [],
    };
    setSlots(prev => [...prev, newSlot]);
  };

  // Drag and drop handlers
  const handleDragStart = (e: React.DragEvent, cohort: Cohort, source: 'palette' | 'slot', slotId?: string) => {
    // The drag payload is JSON rather than `cohortKey`: the key's U+0000 separator is fine in a
    // `<select>` but this string goes out to the browser through `DataTransfer`, and a control
    // character that does not survive that round trip makes the drop parse to null and vanish.
    e.dataTransfer.setData('text/plain', cohortDragPayload(cohort));
    e.dataTransfer.effectAllowed = 'copy';
    setDraggedCohortKey(cohortKey(cohort));
    setDragSource(source);
    if (slotId) setDraggedFromSlotId(slotId);
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  };

  const handleDragEnter = (e: React.DragEvent, slotId: string) => {
    e.preventDefault();
    setDragOverSlotId(slotId);
  };

  const handleDragLeave = (e: React.DragEvent, slotId: string) => {
    // Only clear if actually leaving the slot (not entering a child)
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    if (
      e.clientX < rect.left ||
      e.clientX > rect.right ||
      e.clientY < rect.top ||
      e.clientY > rect.bottom
    ) {
      setDragOverSlotId(null);
    }
  };

  const handleDrop = (e: React.DragEvent, slotId: string) => {
    e.preventDefault();
    const dropped = cohortFromDragPayload(e.dataTransfer.getData('text/plain'));

    // The target slot is the one this handler is bound to, which is the slot the pointer was over
    // when the drop fired -- `onDrop` is per-slot and the event bubbles from whatever was under the
    // pointer. This deliberately does *not* also require `dragOverSlotId === slotId`: that is hover
    // state, set by `dragenter` and cleared by `dragleave`, and gating the drop on it means any
    // missed or out-of-order enter/leave discards a drop that plainly landed on the slot.
    if (dropped) {
      setSlots(prev => prev.map(slot => {
        if (slot.id === slotId) {
          // Check if cohort already exists in this slot
          if (slot.cohorts.some(existing => isSameCohort(existing, dropped))) {
            return slot; // Already in this slot, do nothing
          }
          // Add cohort to slot
          return {
            ...slot,
            cohorts: [...slot.cohorts, dropped],
          };
        }
        return slot;
      }));
    }

    setDragOverSlotId(null);
    setDraggedCohortKey(null);
    setDragSource(null);
    setDraggedFromSlotId(null);
  };

  // Remove cohort from a slot
  const handleRemoveFromSlot = (slotId: string, cohort: Cohort) => {
    setSlots(prev => prev.map(slot => {
      if (slot.id === slotId) {
        return {
          ...slot,
          cohorts: slot.cohorts.filter(existing => !isSameCohort(existing, cohort)),
        };
      }
      return slot;
    }));
  };

  // Save configuration to database
  const handleSave = async () => {
    setIsSaving(true);
    setSaveStatus('idle');
    setSaveError(null);
    
    try {
      // Every request below is checked. A save deletes all existing slots before creating the new
      // ones, so a request that fails without being noticed leaves the configuration half-applied
      // -- and it used to report success anyway, which is how a rejected delete ended up behind a
      // green tick. The status text goes in the error because "save failed" next to a 409 that
      // names a duplicate pair is the difference between an operator giving up and an operator
      // fixing it.
      const expectOk = async (response: Response, action: string) => {
        if (response.ok) return;
        const detail = await response.text();
        throw new Error(`${action} failed (HTTP ${response.status}): ${detail.slice(0, 300)}`);
      };

      // First, get existing slots from backend to know what to delete
      const existingResponse = await fetch(`${API_BASE_URL}/admin/config`, {
        headers: {
          Authorization: `Bearer ${localStorage.getItem('accessToken')}`,
        },
      });
      
      if (!existingResponse.ok) throw new Error('Failed to load existing config');
      
      const existingData = await existingResponse.json();
      const existingSlots = existingData.slots || [];
      
      // Delete all existing slots
      for (const existingSlot of existingSlots) {
        await expectOk(
          await fetch(`${API_BASE_URL}/admin/config/matchmaking-slot`, {
            method: 'DELETE',
            headers: {
              Authorization: `Bearer ${localStorage.getItem('accessToken')}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({ slot_id: existingSlot.id }),
          }),
          'Removing the previous configuration'
        );
      }
      
      // Create new slots from current state
      // For each slot, create all pairs (clique - all-to-all matching)
      // Single-cohort slots create self-pairs (A-A) for self-matching
      const createPair = async (cohortA: Cohort, cohortB: Cohort) => {
        await expectOk(
          await fetch(`${API_BASE_URL}/admin/config/matchmaking-slot`, {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${localStorage.getItem('accessToken')}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({
              year_a: cohortA.year,
              department_a: cohortA.department,
              year_b: cohortB.year,
              department_b: cohortB.department,
            }),
          }),
          `Saving the ${cohortLabel(cohortA.year, cohortA.department)} / ${cohortLabel(cohortB.year, cohortB.department)} pairing`
        );
      };

      for (const slot of slots) {
        if (slot.cohorts.length >= 2) {
          // Create all combinations (clique): for [A,B,C,D] create A-B, A-C, A-D, B-C, B-D, C-D
          for (let i = 0; i < slot.cohorts.length; i++) {
            for (let j = i + 1; j < slot.cohorts.length; j++) {
              await createPair(slot.cohorts[i], slot.cohorts[j]);
            }
          }
        } else if (slot.cohorts.length === 1) {
          // Single cohort slot: create self-pair for self-matching (A-A)
          await createPair(slot.cohorts[0], slot.cohorts[0]);
        }
      }
      
      setSaveStatus('success');
      setSaveError(null);
      setTimeout(() => setSaveStatus('idle'), 3000);
    } catch (error) {
      console.error('Failed to save configuration:', error);
      setSaveStatus('error');
      // Kept, not just logged. A save that deletes the old configuration and then fails partway
      // leaves the operator looking at a screen that no longer matches the database, and "please
      // try again" tells them nothing about which request was refused or why.
      setSaveError(error instanceof Error ? error.message : String(error));
    } finally {
      setIsSaving(false);
    }
  };

  // Initial loading state
  if (isLoading) {
    return (
      <div className="relative min-h-screen bg-background">
        <AdminNavigation />
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-8 px-4 pb-16 pt-28">
          <div className="flex items-center justify-center py-12">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500"></div>
            <span className="ml-3 text-muted-foreground">Loading configuration...</span>
          </div>
        </div>
      </div>
    );
  }

  // Render cohort palette card
  const renderCohortPalette = () => (
    <Card className="border border-border/50">
      <CardHeader className="flex justify-between items-center pb-2 border-b border-border">
        <h2 className="font-medium">Available Cohorts</h2>
        <span className="text-xs text-muted-foreground">
          {allCohorts.length} cohorts • Drag to slots
        </span>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="flex flex-wrap gap-2 min-h-[60px]">
          {allCohorts.map((cohort) => (
            <div
              key={cohortKey(cohort)}
              className={cn(
                'flex items-center gap-2 px-3 py-2 rounded-md border border-border/50',
                'bg-card hover:bg-accent cursor-grab active:cursor-grabbing',
                'select-none transition-colors shadow-sm'
              )}
              draggable={true}
              onDragStart={(e) => handleDragStart(e, cohort, 'palette')}
              style={{ userSelect: 'none' }}
            >
              <span className="font-medium text-sm">{cohortLabel(cohort.year, cohort.department)}</span>
              <svg
                className="w-3 h-3 text-muted-foreground/50"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={2}
                aria-hidden="true"
              >
                <path d="M5 9l-3 3 3 3M9 5l3 3-3 3" />
              </svg>
            </div>
          ))}
          {allCohorts.length === 0 && (
            <p className="text-sm text-muted-foreground">No cohorts found in user table</p>
          )}
        </div>
      </CardContent>
    </Card>
  );

  // Render a single slot box
  const renderSlot = (slot: Slot, index: number) => (
    <div
      key={slot.id}
      ref={(el) => { if (el) slotRefs.current.set(slot.id, el); }}
      className={cn(
        'border rounded-lg p-4 min-h-[100px] flex flex-col gap-3',
        'bg-card/50 border-border/50 transition-all',
        dragOverSlotId === slot.id && 'border-blue-500 bg-blue-50/20 shadow-lg'
      )}
      onDragOver={handleDragOver}
      onDragEnter={(e) => handleDragEnter(e, slot.id)}
      onDragLeave={(e) => handleDragLeave(e, slot.id)}
      onDrop={(e) => handleDrop(e, slot.id)}
    >
      <div className="flex items-center justify-between">
        <span className="font-medium text-sm">
          Slot {index + 1} {slot.id === slots[0].id && slots.length === 1 ? '(Initial)' : ''}
        </span>
        {slots.length > 1 && (
          <button
            onClick={() => setSlots(prev => prev.filter(s => s.id !== slot.id))}
            className="text-red-500 hover:text-red-700 text-sm px-2 py-1 rounded"
            title="Remove slot"
            disabled={slots.length <= 1}
          >
            ✕
          </button>
        )}
      </div>

      <div className="flex flex-wrap gap-1.5 min-h-[40px]">
        {slot.cohorts.length === 0 ? (
          <div className="w-full text-center text-muted-foreground/50 text-sm py-4 border-dashed border-2 rounded">
            Drop cohorts here
          </div>
        ) : (
          slot.cohorts.map((cohort) => {
            return (
              <div
                key={cohortKey(cohort)}
                className={cn(
                  'flex items-center gap-1.5 px-2.5 py-1.5 rounded bg-blue-50 border border-blue-100',
                  'text-sm font-medium text-blue-800'
                )}
                draggable={true}
                onDragStart={(e) => handleDragStart(e, cohort, 'slot', slot.id)}
                onClick={() => handleRemoveFromSlot(slot.id, cohort)}
                style={{ userSelect: 'none', cursor: 'pointer' }}
              >
                <span>{cohortLabel(cohort.year, cohort.department)}</span>
                <svg
                  className="w-3 h-3 opacity-60 hover:opacity-100"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={2}
                  aria-hidden="true"
                >
                  <path d="M18 6L6 18M6 6l12 12" />
                </svg>
              </div>
            );
          })
        )}
      </div>

      {/* Slot hint */}
      <div className="text-xs text-muted-foreground/70">
        {slot.cohorts.length === 0 
          ? 'Drag cohorts from above to create matchmaking rules'
          : slot.cohorts.length === 1
            ? 'Add another cohort to enable matching'
            : `${slot.cohorts.length} cohorts • ${slot.cohorts.length - 1} matchmaking pairs`}
      </div>
    </div>
  );

  return (
    <div className="relative min-h-screen bg-background">
      <AdminNavigation />

      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 pb-16 pt-28">
        <header className="flex flex-col gap-2">
          <h1 className="text-3xl font-semibold">App Configuration</h1>
          <p className="text-sm text-muted-foreground">
            Configure matchmaking slots by dragging cohorts into slots. A cohort is a year and a
            department together, so 2nd-year CSE and 3rd-year CSE are separate cohorts.
            <br />
            A cohort in multiple slots creates multiple matchmaking rules.
            <br />
            A cohort can only appear once per slot but can be in multiple slots.
          </p>
        </header>

        {/* Cohort Palette - always at top */}
        {renderCohortPalette()}

        {/* Slots Grid */}
        <Card className="border border-border/50">
          <CardHeader className="flex justify-between items-center pb-2 border-b border-border">
            <h2 className="font-medium">Matchmaking Slots</h2>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">
                {slots.length} slot{'s'.replace('s', '')}
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={createNewSlot}
                className="h-8 px-3"
                aria-label="Add new empty slot"
              >
                <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                  <path d="M12 5v14M5 12h14" />
                </svg>
                <span className="ml-1">Add Slot</span>
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            {slots.length === 0 ? (
              <div className="flex items-center justify-center py-12 text-muted-foreground">
                <p>No slots configured. Click + to add a slot.</p>
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {slots.map((slot, index) => renderSlot(slot, index))}
              </div>
            )}

            {/* Always show an "add slot" area at the end */}
            <div
              className={cn(
                'border rounded-lg p-4 min-h-[100px] flex items-center justify-center',
                'border-dashed border-border/50 hover:border-blue-300/50 transition-colors cursor-pointer'
              )}
              onClick={createNewSlot}
            >
              <div className="flex flex-col items-center gap-2 text-muted-foreground">
                <svg className="w-8 h-8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
                  <path d="M12 5v14M5 12h14" />
                </svg>
                <span className="text-sm">+ Add New Empty Slot</span>
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Save Button */}
        <Card className="border border-border/50">
          <CardContent className="pt-6 pb-6">
            <Button
              type="button"
              onClick={handleSave}
              disabled={isSaving}
              className="w-full py-3 text-lg"
            >
              {isSaving ? (
                <>
                  <svg className="mr-2 h-5 w-5 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                    <path d="M21 12a9 9 0 1 1-6.219-8.56" />
                  </svg>
                  Saving...
                </>
              ) : (
                'Save Configuration'
              )}
            </Button>
            <p className="text-xs text-muted-foreground text-center mt-2">
              Commit the current slot configuration to the database. This creates matchmaking rules where cohorts in the same slot can match with each other.
            </p>

            {saveStatus === 'success' && (
              <div className="mt-3 p-3 text-sm text-green-700 bg-green-50 rounded-md text-center">
                ✓ Configuration saved successfully!
              </div>
            )}
            {saveStatus === 'error' && (
              <div className="mt-3 p-3 text-sm text-red-700 bg-red-50 rounded-md text-center">
                <div>✗ Failed to save configuration.</div>
                {saveError && (
                  <p className="mt-1 text-xs text-red-600 break-words">{saveError}</p>
                )}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Legend */}
        <Card className="border border-border/50">
          <CardContent className="py-4">
            <h4 className="font-medium mb-2">How it works:</h4>
            <ul className="text-sm text-muted-foreground space-y-1 list-disc list-inside">
              <li>Each <strong>slot</strong> defines a matchmaking group — cohorts within the same slot can match with each other.</li>
              <li>A cohort can appear in <strong>multiple slots</strong> (creating multiple matchmaking rules).</li>
              <li>A cohort can only appear <strong>once per slot</strong>.</li>
              <li>The <strong>initial slot</strong> (first slot) contains all cohorts by default, meaning any cohort can match with any other.</li>
              <li>Click a cohort in a slot to remove it. Drag from the palette above to add cohorts to slots.</li>
            </ul>
          </CardContent>
        </Card>
      </div>
    </div>
  );
};

export default AppConfiguration;