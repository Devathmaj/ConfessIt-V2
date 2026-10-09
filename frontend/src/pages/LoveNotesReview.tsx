import { useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { AdminNavigation } from '@/components/AdminNavigation';
import { toast } from 'sonner';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Label } from '@/components/ui/label';
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/accordion';
import {
  getAdminLoveNotes,
  retryAdminLoveNoteRedaction,
  updateAdminLoveNoteStatus,
} from '@/services/api';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { formatDateTimeDDMMYYYY, timestampMs } from '@/lib/utils';
import { EyeOff, ShieldCheck, XCircle } from 'lucide-react';

/** What the moderation list returns: the note's redacted words and the fields needed to act
 * on it. No sender, no recipient, no image, no anonymity flag -- see
 * ``get_admin_love_notes_data`` for why the review screen never receives identities. */
interface AdminLoveNote {
  id: string;
  /** The PII-redacted rendering of the note, produced by the backend when it was sent.
   * ``null`` means no preview could be produced (the redaction provider failed); the
   * screen then shows that state with a retry and never the original text, which it does
   * not receive at all. */
  moderation_body: string | null;
  status: 'approved' | 'rejected' | 'pending_review';
  created_at: string | null;
  content_removed?: boolean;
  removed_at?: string | null;
  removed_by_regno?: string | null;
}

/** One row of the report queue lives on the Reports Review screen, next to the confession and
 * chat queues -- a note report is a report, and splitting reports by which moderation screen
 * happens to own the content means a reviewer has to know which tab to open before knowing
 * whether there is anything to review. */

type NoteStatus = AdminLoveNote['status'];
type OrderFilter = 'newest' | 'oldest';

/**
 * How many notes one section read returns. The backend clamps to 100; twenty keeps one
 * read small while a "load more" click still grows the list by a visible amount.
 */
const SECTION_PAGE_SIZE = 20;

const STATUS_ORDER: NoteStatus[] = ['pending_review', 'rejected', 'approved'];

/** The rows inside a `getAdminLoveNotes` answer, whether it is the page envelope or a legacy bare array. */
const notesOf = (answer: unknown): AdminLoveNote[] => {
  if (Array.isArray(answer)) return answer as AdminLoveNote[];
  const notes = (answer as { notes?: AdminLoveNote[] } | null | undefined)?.notes;
  return Array.isArray(notes) ? notes : [];
};

/** The matching-row count beside a `getAdminLoveNotes` answer, falling back to the rows at hand. */
const totalOf = (answer: unknown, fallback: number): number => {
  if (Array.isArray(answer)) return answer.length;
  const total = (answer as { total?: unknown } | null | undefined)?.total;
  return typeof total === 'number' && Number.isFinite(total) ? total : fallback;
};

const normalizeNote = (item: Partial<AdminLoveNote>, index: number): AdminLoveNote => ({
  id: item.id ?? `love-note-${index}`,
  moderation_body: typeof item.moderation_body === 'string' ? item.moderation_body : null,
  status: (item.status as AdminLoveNote['status']) ?? 'pending_review',
  created_at: item.created_at ?? null,
  content_removed: item.content_removed ?? false,
  removed_at: item.removed_at ?? null,
  removed_by_regno: item.removed_by_regno ?? null,
});

interface SectionWindow {
  /** Notes loaded so far: always the contiguous window [0, items.length) of the server order. */
  items: AdminLoveNote[];
  /** How many notes match this section's filter on the server, loaded or not. */
  total: number;
}

const emptyWindow = (): SectionWindow => ({ items: [], total: 0 });

const formatTimestamp = (value: string | null) => {
  return formatDateTimeDDMMYYYY(value);
};

const statusConfig: Record<AdminLoveNote['status'], { label: string; variant: 'default' | 'secondary' | 'destructive' }> = {
  approved: { label: 'Approved', variant: 'default' },
  rejected: { label: 'Rejected', variant: 'destructive' },
  pending_review: { label: 'Pending review', variant: 'secondary' },
};

export const LoveNotesReview = () => {
  const [sections, setSections] = useState<Record<NoteStatus, SectionWindow>>({
    pending_review: emptyWindow(),
    rejected: emptyWindow(),
    approved: emptyWindow(),
  });
  /**
   * The one unfiltered list, only ever set from legacy bare-array answers (older servers
   * and unit-test mocks), which ignore request params. The real server answers envelopes
   * and stays `null`, which is what switches the screen from client splitting to server
   * pages: three reads that each returned the same full list would otherwise triple-count
   * every total.
   */
  const [legacyRows, setLegacyRows] = useState<AdminLoveNote[] | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  /** A filter change, order flip or moderation action re-reading while the lists stay up. */
  const [refreshing, setRefreshing] = useState(false);
  /** Which section is appending its next page, so only its button reads busy. */
  const [loadingMore, setLoadingMore] = useState<NoteStatus | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [selectedNoteId, setSelectedNoteId] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<'all' | AdminLoveNote['status']>('all');
  /** Timeline order every section read is issued with: newest or oldest arrivals first. */
  const [orderFilter, setOrderFilter] = useState<OrderFilter>('newest');
  /** Whether the first load is still in flight, which is the only one that takes the loading screen. */
  const isFirstLoad = useRef(true);

  const statusSections: Array<{ key: AdminLoveNote['status']; heading: string; emptyLabel: string }> = useMemo(
    () => [
      { key: 'pending_review', heading: 'Pending review', emptyLabel: 'No notes awaiting review.' },
      { key: 'rejected', heading: 'Rejected notes', emptyLabel: 'No notes have been rejected.' },
      { key: 'approved', heading: 'Accepted notes', emptyLabel: 'No notes have been approved yet.' },
    ],
    []
  );

  /**
   * One section read: the next `limit` notes of one moderation state in the chosen
   * timeline order. The status filter is a SQL WHERE and the page a SQL LIMIT/OFFSET, so
   * the backend reads at most one page no matter how many notes exist.
   */
  const readSection = async (
    status: NoteStatus,
    skip: number,
    limit: number,
    order: OrderFilter,
  ): Promise<SectionWindow> => {
    const answer = await getAdminLoveNotes({ status, order, skip, limit });
    return {
      items: notesOf(answer).map((item, index) => normalizeNote(item, index)),
      total: totalOf(answer, 0),
    };
  };

  /**
   * First page of every section, freshly: what an order flip and the first load do. (The
   * status picker only chooses which sections render, so it needs no refetch.)
   */
  useEffect(() => {
    const cancelled = { current: false };
    let active = true;
    const fetchNotes = async () => {
      if (isFirstLoad.current) setLoading(true);
      else setRefreshing(true);
      try {
        // Probe the answer shape with the three section reads: envelopes stay per-section
        // server pages, while three identical bare arrays collapse into the legacy list.
        const raw = await Promise.all(
          STATUS_ORDER.map((status) =>
            getAdminLoveNotes({ status, order: orderFilter, skip: 0, limit: SECTION_PAGE_SIZE }),
          ),
        );
        if (!active || cancelled.current) return;
        if (raw.every((answer) => Array.isArray(answer))) {
          const seen = new Set<string>();
          const merged: AdminLoveNote[] = [];
          raw.forEach((answer, readIndex) => {
            notesOf(answer).forEach((item, index) => {
              const note = normalizeNote(item, readIndex * SECTION_PAGE_SIZE + index);
              if (!note.id || seen.has(note.id)) return;
              seen.add(note.id);
              merged.push(note);
            });
          });
          setLegacyRows(merged);
        } else {
          setLegacyRows(null);
          setSections({
            pending_review: {
              items: notesOf(raw[0]).map((item, index) => normalizeNote(item, index)),
              total: totalOf(raw[0], 0),
            },
            rejected: {
              items: notesOf(raw[1]).map((item, index) => normalizeNote(item, index)),
              total: totalOf(raw[1], 0),
            },
            approved: {
              items: notesOf(raw[2]).map((item, index) => normalizeNote(item, index)),
              total: totalOf(raw[2], 0),
            },
          });
        }
      } catch (error) {
        if (active && !cancelled.current) {
          console.error('Failed to load love notes', error);
          toast.error('Unable to load love notes for review.');
        }
      } finally {
        if (active && !cancelled.current) {
          setLoading(false);
          setRefreshing(false);
          isFirstLoad.current = false;
        }
      }
    };

    fetchNotes();
    return () => {
      active = false;
      cancelled.current = true;
    };
  }, [orderFilter]);

  /**
   * The legacy split: one list divided by status, newest or oldest first.
   *
   * Only read when the answers were bare arrays. Against the server the sections below
   * already arrive split and ordered, and re-sorting them here would undo the timeline
   * order the reader picked.
   */
  const legacyGrouped = useMemo(() => {
    if (!legacyRows) return null;
    const base: Record<AdminLoveNote['status'], AdminLoveNote[]> = {
      pending_review: [],
      rejected: [],
      approved: [],
    };
    const direction = orderFilter === 'newest' ? -1 : 1;
    const sorted = [...legacyRows].sort(
      (a, b) => direction * (timestampMs(b.created_at) - timestampMs(a.created_at)),
    );
    sorted.forEach((note) => {
      base[note.status]?.push(note);
    });
    return base;
  }, [legacyRows, orderFilter]);

  const serverDriven = legacyGrouped === null;

  const notesFor = (status: NoteStatus): AdminLoveNote[] =>
    serverDriven ? sections[status].items : (legacyGrouped?.[status] ?? []);

  const totalFor = (status: NoteStatus): number =>
    serverDriven ? sections[status].total : (legacyGrouped?.[status].length ?? 0);

  const pendingCount = totalFor('pending_review');
  const allTotal = STATUS_ORDER.reduce((sum, status) => sum + totalFor(status), 0);

  const sectionsToRender = useMemo(() => {
    if (statusFilter === 'all') {
      return statusSections;
    }
    return statusSections.filter((section) => section.key === statusFilter);
  }, [statusFilter, statusSections]);

  const renderedCount = useMemo(
    () => sectionsToRender.reduce((total, section) => total + notesFor(section.key).length, 0),
    // `notesFor` reads the section windows; listing them keeps the count honest.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sections, legacyGrouped, sectionsToRender],
  );

  const renderedTotal = useMemo(
    () => sectionsToRender.reduce((total, section) => total + totalFor(section.key), 0),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sections, legacyGrouped, sectionsToRender],
  );

  /**
   * Grow one section downwards: the next page appends beneath the notes already on
   * screen, so the reader's place never moves. The window stays contiguous from zero,
   * which is what keeps a later refresh a single clean re-read.
   */
  const loadMore = async (status: NoteStatus) => {
    if (loadingMore !== null || refreshing) return;
    setLoadingMore(status);
    try {
      const window = await readSection(status, sections[status].items.length, SECTION_PAGE_SIZE, orderFilter);
      setSections((prev) => ({
        ...prev,
        [status]: { items: [...prev[status].items, ...window.items], total: window.total },
      }));
    } catch (error) {
      console.error('Failed to load more love notes', error);
      toast.error('Unable to load more love notes.');
    } finally {
      setLoadingMore(null);
    }
  };

  /**
   * Re-read the sections an action touched, across the whole window currently loaded.
   *
   * A moderation action moves a note between sections, so patching one card's status
   * locally would leave both counts stale and the moved note sitting in the wrong
   * section's page. Re-reading [0, loaded) of each touched section pulls the freshly
   * modified rows back in their new places with true totals beside them.
   */
  const refreshSections = async (statuses: NoteStatus[]) => {
    setRefreshing(true);
    try {
      const fresh = await Promise.all(
        statuses.map((status) =>
          readSection(status, 0, Math.max(sections[status].items.length, SECTION_PAGE_SIZE), orderFilter),
        ),
      );
      setSections((prev) => {
        const next = { ...prev };
        statuses.forEach((status, index) => {
          next[status] = fresh[index];
        });
        return next;
      });
    } catch (error) {
      console.error('Failed to refresh love notes', error);
      toast.error('Unable to refresh the love notes.');
    } finally {
      setRefreshing(false);
    }
  };

  const findNoteStatus = (noteId: string): NoteStatus | null => {
    if (!serverDriven) {
      return legacyRows?.find((note) => note.id === noteId)?.status ?? null;
    }
    for (const status of STATUS_ORDER) {
      if (sections[status].items.some((note) => note.id === noteId)) return status;
    }
    return null;
  };

  const applyStatus = async (noteId: string, status: AdminLoveNote['status']) => {
    const from = findNoteStatus(noteId);
    try {
      await updateAdminLoveNoteStatus(noteId, status);
      if (!serverDriven) {
        // Legacy single list: the move is local, exactly as before pagination existed.
        setLegacyRows((prev) =>
          prev ? prev.map((item) => (item.id === noteId ? { ...item, status } : item)) : prev,
        );
      } else if (from && from !== status) {
        await refreshSections([from, status]);
      } else {
        await refreshSections([status]);
      }
      toast.success(status === 'approved' ? 'Love note approved.' : 'Love note hidden from the recipient.');
    } catch (error) {
      console.error('Failed to update love note', error);
      toast.error('Unable to update this love note.');
    }
  };

  /** Hiding is only ever an approved note going back to ``rejected``, which is the status the
   * recipient's inbox already filters on -- so the note leaves their view without the row ever
   * being deleted. The row is not recoverable afterwards: a rejection is final and the server
   * refuses to approve one, which is why the confirmation says "for good" rather than
   * promising a way back. */
  const askHide = (noteId: string) => {
    setSelectedNoteId(noteId);
    setDialogOpen(true);
  };

  const confirmHide = async () => {
    if (!selectedNoteId) return;
    try {
      await applyStatus(selectedNoteId, 'rejected');
    } finally {
      setDialogOpen(false);
      setSelectedNoteId(null);
    }
  };

  /** Which note's preview is regenerating, so only that row's button reads busy.
   *
   * A note with no preview was still accepted at send time -- the sender's note is not
   * held hostage to the redaction provider -- and this is how staff ask for the preview
   * again. The response carries the redacted text only, which is patched into whichever
   * list holds the row; nothing on this screen can ever receive the raw note. */
  const [redacting, setRedacting] = useState<string | null>(null);

  const retryRedaction = async (noteId: string) => {
    if (redacting !== null) return;
    setRedacting(noteId);
    try {
      const { moderation_body } = await retryAdminLoveNoteRedaction(noteId);
      const patch = (items: AdminLoveNote[]) =>
        items.map((item) =>
          item.id === noteId ? { ...item, moderation_body: moderation_body ?? item.moderation_body } : item,
        );
      setSections((prev) => {
        const next = { ...prev };
        (Object.keys(next) as NoteStatus[]).forEach((status) => {
          next[status] = { ...next[status], items: patch(next[status].items) };
        });
        return next;
      });
      setLegacyRows((prev) => (prev ? patch(prev) : prev));
      toast.success('Privacy-safe preview generated.');
    } catch (error) {
      console.error('Failed to generate the moderation preview', error);
      toast.error('Unable to generate the moderation preview. Please try again shortly.');
    } finally {
      setRedacting(null);
    }
  };

  return (
    <div className="relative min-h-screen bg-background">
      <AdminNavigation />

      <div className="mx-auto flex w-full max-w-6xl flex-col gap-8 px-4 pb-16 pt-28">
        <header className="flex flex-col gap-2">
          <h1 className="text-3xl font-semibold">Love Notes Review</h1>
          <p className="text-sm text-muted-foreground">
            Review what each love note says. Sender, recipient and image are not shown.
          </p>
          <p className="text-xs text-muted-foreground/80">
            Total notes: {allTotal} · Pending decisions: {pendingCount}
            {refreshing ? ' · Updating…' : ''}
          </p>
        </header>

        <div className="space-y-8">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap items-center gap-4">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Label
                htmlFor="love-notes-status-filter"
                className="uppercase tracking-wide"
              >
                Filter
              </Label>
              <Select value={statusFilter} onValueChange={(value) => setStatusFilter(value as typeof statusFilter)}>
                <SelectTrigger id="love-notes-status-filter" className="w-44">
                  <SelectValue placeholder="Status" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All statuses</SelectItem>
                  <SelectItem value="pending_review">Pending</SelectItem>
                  <SelectItem value="approved">Approved</SelectItem>
                  <SelectItem value="rejected">Rejected</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Label
                htmlFor="love-notes-order-filter"
                className="uppercase tracking-wide"
              >
                Order
              </Label>
              <Select value={orderFilter} onValueChange={(value) => setOrderFilter(value as OrderFilter)}>
                <SelectTrigger id="love-notes-order-filter" className="w-44">
                  <SelectValue placeholder="Order" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="newest">Latest first</SelectItem>
                  <SelectItem value="oldest">Oldest first</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="flex flex-col items-start gap-1 sm:items-end">
            <Badge variant="secondary">
              Showing {renderedCount} of {renderedTotal}
            </Badge>
            <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
              {statusSections.map((section) => (
                <span key={section.key}>
                  {section.heading}: {totalFor(section.key)}
                </span>
              ))}
            </div>
          </div>
        </div>

        {loading ? (
          <Card>
            <CardContent className="py-12 text-center text-sm text-muted-foreground">
              Loading love notes for review...
            </CardContent>
          </Card>
        ) : renderedCount === 0 ? (
          <Card>
            <CardContent className="py-12 text-center text-sm text-muted-foreground">
              No love notes in this state.
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-6">
            {sectionsToRender.map((section) => {
              const notesForSection = notesFor(section.key);
              const total = totalFor(section.key);
              const statusMeta = statusConfig[section.key];
              const remaining = total - notesForSection.length;
              return (
                <section key={section.key} className="space-y-3">
                  <div className="flex items-center justify-between">
                    <h2 className="text-lg font-semibold">{section.heading}</h2>
                    <Badge variant={statusMeta.variant}>
                      {notesForSection.length} of {total} {total === 1 ? 'note' : 'notes'}
                    </Badge>
                  </div>
                  {notesForSection.length === 0 ? (
                    <Card>
                      <CardContent className="py-10 text-center text-sm text-muted-foreground">
                        {section.emptyLabel}
                      </CardContent>
                    </Card>
                  ) : (
                    <Accordion type="single" collapsible className="space-y-3">
                      {notesForSection.map((note) => {
                        const currentStatus = statusConfig[note.status];
                        return (
                          <AccordionItem
                            key={note.id}
                            value={note.id}
                            className="overflow-hidden rounded-2xl border border-border bg-muted/20"
                          >
                            <AccordionTrigger className="flex w-full flex-wrap items-start justify-between gap-3 px-4 py-3 text-left">
                              <div className="flex flex-col gap-1">
                                <span className="text-sm font-semibold">
                                  {currentStatus.label}
                                  {note.content_removed ? ' · removed' : ''}
                                </span>
                                <span className="text-xs text-muted-foreground">
                                  Created {formatTimestamp(note.created_at)}
                                </span>
                              </div>
                              <Badge variant={currentStatus.variant}>{currentStatus.label}</Badge>
                            </AccordionTrigger>
                            <AccordionContent className="space-y-5 bg-background/40 px-4 pb-5 pt-2">
                              <div className="rounded-2xl border border-border bg-background/60 p-4">
                                <p className="text-sm uppercase tracking-wide text-muted-foreground">Message</p>
                                {/* The reviewer reads the padlock, not the safe: this is the
                                    PII-redacted preview and it is the only rendering of the
                                    note this screen has. When no preview could be produced
                                    the row says so and offers a retry -- it never silently
                                    falls back to anything, because there is nothing else
                                    here to fall back to. */}
                                {note.moderation_body !== null ? (
                                  <p className="mt-2 whitespace-pre-line text-base leading-relaxed">
                                    {note.moderation_body}
                                  </p>
                                ) : (
                                  <div className="mt-2 space-y-2">
                                    <p className="text-sm text-muted-foreground">
                                      No privacy-safe preview is available for this note. The original
                                      text is never shown on this screen.
                                    </p>
                                    <Button
                                      variant="outline"
                                      size="sm"
                                      onClick={() => retryRedaction(note.id)}
                                      disabled={redacting !== null}
                                    >
                                      {redacting === note.id ? 'Generating…' : 'Generate preview'}
                                    </Button>
                                  </div>
                                )}
                              </div>

                              {note.content_removed ? (
                                <div className="rounded-2xl border border-border bg-muted/20 p-4 text-xs text-muted-foreground">
                                  <p className="font-medium text-foreground/80">Removed</p>
                                  <p>
                                    Taken down {formatTimestamp(note.removed_at)}
                                    {note.removed_by_regno ? ` by ${note.removed_by_regno}` : ''}
                                  </p>
                                </div>
                              ) : null}

                              <div className="flex flex-wrap items-center justify-end gap-2">
                                {/* One action per state, never a set of three. An approved note
                                    is already delivered, so the only thing left to decide about
                                    it is whether it stays that way -- Hide note takes it back
                                    out of the inbox and cannot be undone. A pending note can be
                                    approved or rejected. A rejected note offers nothing at all,
                                    because the rejection is final: the endpoint refuses it, and
                                    offering a button that would 409 would be worse than having
                                    none. There is no "back to pending" either -- a note that has
                                    been ruled on has been ruled on. */}
                                {note.status === 'approved' ? (
                                  <Button
                                    variant="outline"
                                    size="sm"
                                    className="border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground"
                                    onClick={() => askHide(note.id)}
                                  >
                                    <EyeOff className="mr-2 h-4 w-4" /> Hide note
                                  </Button>
                                ) : note.status === 'pending_review' ? (
                                  <>
                                    <Button
                                      variant="outline"
                                      size="sm"
                                      className="border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground"
                                      onClick={() => applyStatus(note.id, 'rejected')}
                                    >
                                      <XCircle className="mr-2 h-4 w-4" /> Reject
                                    </Button>
                                    <Button
                                      variant="outline"
                                      size="sm"
                                      className="border-green-500 text-green-500 hover:bg-green-500 hover:text-white"
                                      onClick={() => applyStatus(note.id, 'approved')}
                                    >
                                      <ShieldCheck className="mr-2 h-4 w-4" /> Approve
                                    </Button>
                                  </>
                                ) : null}
                              </div>
                            </AccordionContent>
                          </AccordionItem>
                        );
                      })}
                    </Accordion>
                  )}
                  {/* The list only grows downwards: the next page appends beneath the notes
                      already read, so the reader's place never moves and the accordion they
                      have open stays open. Shown only while the server holds more than the
                      section is showing. */}
                  {serverDriven && remaining > 0 && (
                    <div className="flex justify-center pt-1">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => loadMore(section.key)}
                        disabled={loadingMore !== null || refreshing}
                        aria-label={`Show more ${section.heading.toLowerCase()}`}
                      >
                        {loadingMore === section.key
                          ? 'Loading…'
                          : `Show more (${remaining} remaining)`}
                      </Button>
                    </div>
                  )}
                </section>
              );
            })}
          </div>
        )}
        </div>
      </div>

      <AlertDialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Hide this love note?</AlertDialogTitle>
            <AlertDialogDescription>
              This takes the note out of the recipient&apos;s inbox for good. A rejection is
              final, so the note cannot be approved again afterwards, and the sender is told
              it was taken down.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmHide} className="bg-destructive hover:bg-destructive/90">
              Hide note
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      </div>
  );
};
