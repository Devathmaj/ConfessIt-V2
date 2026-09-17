import { AdminNavigation } from '@/components/AdminNavigation';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { useEffect, useState, useRef } from 'react';
import { cn } from '@/lib/utils';

// Base URL for backend API
const API_BASE_URL = 'http://localhost:8001';

interface Slot {
  id: string;
  classes: string[];  // Array of class names in this slot
}

/**
 * Operator-facing matchmaking slot configuration with drag-and-drop UI.
 *
 * - Class palette at top: all unique classes from user table (infinite supply)
 * - Initially one slot box containing all classes (represents "any class can match with any")
 * - "+" button creates new empty slot boxes
 * - Drag class cards from palette into slot boxes
 * - A class can be in multiple slots but only once per slot
 * - Save button commits configuration to database
 */
export const AppConfiguration = () => {
  const [allClasses, setAllClasses] = useState<string[]>([]);
  const [slots, setSlots] = useState<Slot[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [dragSource, setDragSource] = useState<'palette' | 'slot' | null>(null);
  const [draggedClass, setDraggedClass] = useState<string | null>(null);
  const [draggedFromSlotId, setDraggedFromSlotId] = useState<string | null>(null);
  const [dragOverSlotId, setDragOverSlotId] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<'idle' | 'success' | 'error'>('idle');

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
        
        setAllClasses(data.classes || []);
        
        // Convert backend slots (pairs) to frontend slots (arrays of classes)
        const backendSlots = data.slots || [];
        
        if (backendSlots.length === 0 && data.classes && data.classes.length > 0) {
          // No slots yet, create initial slot with all classes
          const initialSlot: Slot = {
            id: 'initial-' + Date.now(),
            classes: [...data.classes],
          };
          setSlots([initialSlot]);
        } else {
          // Reconstruct slots from pairs
          // Pairs are saved as (first_class, other_class) where first_class is the first class in the slot
          // Group by class_a to reconstruct slots
          const slotMap = new Map<string, Set<string>>();
          
          for (const pair of backendSlots) {
            const { class_a, class_b, id } = pair;
            if (!slotMap.has(class_a)) {
              slotMap.set(class_a, new Set());
            }
            slotMap.get(class_a)!.add(class_a);
            slotMap.get(class_a)!.add(class_b);
          }
          
          const reconstructedSlots: Slot[] = Array.from(slotMap.entries()).map(([classA, classSet], index) => ({
            id: `reconstructed-${classA}-${index}`,
            classes: Array.from(classSet).sort(),
          }));
          
          // If no slots reconstructed but classes exist, create initial slot
          if (reconstructedSlots.length === 0 && data.classes && data.classes.length > 0) {
            setSlots([{
              id: 'initial-' + Date.now(),
              classes: [...data.classes],
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
      classes: [],
    };
    setSlots(prev => [...prev, newSlot]);
  };

  // Drag and drop handlers
  const handleDragStart = (e: React.DragEvent, className: string, source: 'palette' | 'slot', slotId?: string) => {
    e.dataTransfer.setData('text/plain', className);
    e.dataTransfer.effectAllowed = 'copy';
    setDraggedClass(className);
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
    const className = e.dataTransfer.getData('text/plain');
    
    if (className && dragOverSlotId === slotId) {
      setSlots(prev => prev.map(slot => {
        if (slot.id === slotId) {
          // Check if class already exists in this slot
          if (slot.classes.includes(className)) {
            return slot; // Already in this slot, do nothing
          }
          // Add class to slot
          return {
            ...slot,
            classes: [...slot.classes, className],
          };
        }
        return slot;
      }));
    }
    
    setDragOverSlotId(null);
    setDraggedClass(null);
    setDragSource(null);
    setDraggedFromSlotId(null);
  };

  // Remove class from a slot
  const handleRemoveFromSlot = (slotId: string, className: string) => {
    setSlots(prev => prev.map(slot => {
      if (slot.id === slotId) {
        return {
          ...slot,
          classes: slot.classes.filter(c => c !== className),
        };
      }
      return slot;
    }));
  };

  // Save configuration to database
  const handleSave = async () => {
    setIsSaving(true);
    setSaveStatus('idle');
    
    try {
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
        await fetch(`${API_BASE_URL}/admin/config/matchmaking-slot`, {
          method: 'DELETE',
          headers: {
            Authorization: `Bearer ${localStorage.getItem('accessToken')}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ slotId: existingSlot.id }),
        });
      }
      
      // Create new slots from current state
      // For each slot, create all pairs (clique - all-to-all matching)
      // Single-class slots create self-pairs (A-A) for self-matching
      for (const slot of slots) {
        if (slot.classes.length >= 2) {
          // Create all combinations (clique): for [A,B,C,D] create A-B, A-C, A-D, B-C, B-D, C-D
          for (let i = 0; i < slot.classes.length; i++) {
            for (let j = i + 1; j < slot.classes.length; j++) {
              const classA = slot.classes[i];
              const classB = slot.classes[j];
              
              await fetch(`${API_BASE_URL}/admin/config/matchmaking-slot`, {
                method: 'POST',
                headers: {
                  Authorization: `Bearer ${localStorage.getItem('accessToken')}`,
                  'Content-Type': 'application/json',
                },
                body: JSON.stringify({ class_a: classA, class_b: classB }),
              });
            }
          }
        } else if (slot.classes.length === 1) {
          // Single class slot: create self-pair for self-matching (A-A)
          const classA = slot.classes[0];
          
          await fetch(`${API_BASE_URL}/admin/config/matchmaking-slot`, {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${localStorage.getItem('accessToken')}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({ class_a: classA, class_b: classA }),
          });
        }
      }
      
      setSaveStatus('success');
      setTimeout(() => setSaveStatus('idle'), 3000);
    } catch (error) {
      console.error('Failed to save configuration:', error);
      setSaveStatus('error');
      setTimeout(() => setSaveStatus('idle'), 3000);
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

  // Render class palette card
  const renderClassPalette = () => (
    <Card className="border border-border/50">
      <CardHeader className="flex justify-between items-center pb-2 border-b border-border">
        <h2 className="font-medium">Available Classes</h2>
        <span className="text-xs text-muted-foreground">
          {allClasses.length} classes • Drag to slots
        </span>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="flex flex-wrap gap-2 min-h-[60px]">
          {allClasses.map((cls, index) => (
            <div
              key={index}
              className={cn(
                'flex items-center gap-2 px-3 py-2 rounded-md border border-border/50',
                'bg-card hover:bg-accent cursor-grab active:cursor-grabbing',
                'select-none transition-colors shadow-sm'
              )}
              draggable={true}
              onDragStart={(e) => handleDragStart(e, cls, 'palette')}
              style={{ userSelect: 'none' }}
            >
              <span className="font-medium text-sm">{cls}</span>
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
          {allClasses.length === 0 && (
            <p className="text-sm text-muted-foreground">No classes found in user table</p>
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
      onDragLeave={handleDragLeave}
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
        {slot.classes.length === 0 ? (
          <div className="w-full text-center text-muted-foreground/50 text-sm py-4 border-dashed border-2 rounded">
            Drop classes here
          </div>
        ) : (
          slot.classes.map((cls, clsIndex) => {
            return (
              <div
                key={clsIndex}
                className={cn(
                  'flex items-center gap-1.5 px-2.5 py-1.5 rounded bg-blue-50 border border-blue-100',
                  'text-sm font-medium text-blue-800'
                )}
                draggable={true}
                onDragStart={(e) => handleDragStart(e, cls, 'slot', slot.id)}
                onClick={() => handleRemoveFromSlot(slot.id, cls)}
                style={{ userSelect: 'none', cursor: 'pointer' }}
              >
                <span>{cls}</span>
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
        {slot.classes.length === 0 
          ? 'Drag classes from above to create matchmaking rules'
          : slot.classes.length === 1
            ? 'Add another class to enable matching'
            : `${slot.classes.length} classes • ${slot.classes.length - 1} matchmaking pairs`}
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
            Configure matchmaking slots by dragging classes into slots.
            <br />
            A class in multiple slots creates multiple matchmaking rules.
            <br />
            A class can only appear once per slot but can be in multiple slots.
          </p>
        </header>

        {/* Class Palette - always at top */}
        {renderClassPalette()}

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
              Commit the current slot configuration to the database. This creates matchmaking rules where classes in the same slot can match with each other.
            </p>

            {saveStatus === 'success' && (
              <div className="mt-3 p-3 text-sm text-green-700 bg-green-50 rounded-md text-center">
                ✓ Configuration saved successfully!
              </div>
            )}
            {saveStatus === 'error' && (
              <div className="mt-3 p-3 text-sm text-red-700 bg-red-50 rounded-md text-center">
                ✗ Failed to save configuration. Please try again.
              </div>
            )}
          </CardContent>
        </Card>

        {/* Legend */}
        <Card className="border border-border/50">
          <CardContent className="py-4">
            <h4 className="font-medium mb-2">How it works:</h4>
            <ul className="text-sm text-muted-foreground space-y-1 list-disc list-inside">
              <li>Each <strong>slot</strong> defines a matchmaking group — classes within the same slot can match with each other.</li>
              <li>A class can appear in <strong>multiple slots</strong> (creating multiple matchmaking rules).</li>
              <li>A class can only appear <strong>once per slot</strong>.</li>
              <li>The <strong>initial slot</strong> (first slot) contains all classes by default, meaning any class can match with any other.</li>
              <li>Click a class in a slot to remove it. Drag from the palette above to add classes to slots.</li>
            </ul>
          </CardContent>
        </Card>
      </div>
    </div>
  );
};

export default AppConfiguration;