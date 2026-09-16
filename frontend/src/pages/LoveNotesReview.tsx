import { useEffect, useMemo, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
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
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/accordion';
import {
  getAdminLoveNotes,
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

/** What the moderation list returns: the note's words and the fields needed to act on it.
 * No sender, no recipient, no image, no anonymity flag -- see
 * ``get_all_love_notes_for_admin`` for why the review screen never receives identities. */
interface AdminLoveNote {
  id: string;
  body: string;
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

const formatTimestamp = (value: string | null) => {
  return formatDateTimeDDMMYYYY(value);
};

const statusConfig: Record<AdminLoveNote['status'], { label: string; variant: 'default' | 'secondary' | 'destructive' }> = {
  approved: { label: 'Approved', variant: 'default' },
  rejected: { label: 'Rejected', variant: 'destructive' },
  pending_review: { label: 'Pending review', variant: 'secondary' },
};

export const LoveNotesReview = () => {
  const [notes, setNotes] = useState<AdminLoveNote[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [selectedNoteId, setSelectedNoteId] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<'all' | AdminLoveNote['status']>('all');

  const statusSections: Array<{ key: AdminLoveNote['status']; heading: string; emptyLabel: string }> = useMemo(
    () => [
      { key: 'pending_review', heading: 'Pending review', emptyLabel: 'No notes awaiting review.' },
      { key: 'rejected', heading: 'Rejected notes', emptyLabel: 'No notes have been rejected.' },
      { key: 'approved', heading: 'Accepted notes', emptyLabel: 'No notes have been approved yet.' },
    ],
    []
  );

  useEffect(() => {
    const fetchNotes = async () => {
      try {
        setLoading(true);
        const response = await getAdminLoveNotes();
        const normalized = Array.isArray(response)
          ? response.map((item: Partial<AdminLoveNote>, index) => ({
              id: item.id ?? `love-note-${index}`,
              body: item.body ?? '',
              status: (item.status as AdminLoveNote['status']) ?? 'pending_review',
              created_at: item.created_at ?? null,
              content_removed: item.content_removed ?? false,
              removed_at: item.removed_at ?? null,
              removed_by_regno: item.removed_by_regno ?? null,
            }))
          : [];
        setNotes(normalized);
      } catch (error) {
        console.error('Failed to load love notes', error);
        toast.error('Unable to load love notes for review.');
      } finally {
        setLoading(false);
      }
    };

    fetchNotes();
  }, []);

  const pendingCount = useMemo(
    () => notes.filter((note) => note.status === 'pending_review').length,
    [notes]
  );

  const groupedNotes = useMemo(() => {
    const base: Record<AdminLoveNote['status'], AdminLoveNote[]> = {
      pending_review: [],
      rejected: [],
      approved: [],
    };
    const sorted = [...notes].sort((a, b) => {
      const aTime = timestampMs(a.created_at);
      const bTime = timestampMs(b.created_at);
      return bTime - aTime;
    });
    sorted.forEach((note) => {
      base[note.status]?.push(note);
    });
    return base;
  }, [notes]);

  const sectionsToRender = useMemo(() => {
    if (statusFilter === 'all') {
      return statusSections;
    }
    return statusSections.filter((section) => section.key === statusFilter);
  }, [statusFilter, statusSections]);

  const renderedCount = useMemo(
    () => sectionsToRender.reduce((total, section) => total + groupedNotes[section.key].length, 0),
    [groupedNotes, sectionsToRender]
  );

  const applyStatus = async (noteId: string, status: AdminLoveNote['status']) => {
    try {
      await updateAdminLoveNoteStatus(noteId, status);
      setNotes((prev) => prev.map((item) => (item.id === noteId ? { ...item, status } : item)));
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
            Total notes: {notes.length} · Pending decisions: {pendingCount}
          </p>
        </header>

        <div className="space-y-8">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className="uppercase tracking-wide">Filter</span>
            <Select value={statusFilter} onValueChange={(value) => setStatusFilter(value as typeof statusFilter)}>
              <SelectTrigger className="w-44">
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
          <div className="flex flex-col items-start gap-1 sm:items-end">
            <Badge variant="secondary">
              Showing {renderedCount} of {notes.length}
            </Badge>
            <div className="flex flex-wrap gap-2 text-[11px] text-muted-foreground">
              {statusSections.map((section) => (
                <span key={section.key}>
                  {section.heading}: {groupedNotes[section.key].length}
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
              const notesForSection = groupedNotes[section.key];
              const statusMeta = statusConfig[section.key];
              return (
                <section key={section.key} className="space-y-3">
                  <div className="flex items-center justify-between">
                    <h2 className="text-lg font-semibold">{section.heading}</h2>
                    <Badge variant={statusMeta.variant}>
                      {notesForSection.length} {notesForSection.length === 1 ? 'note' : 'notes'}
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
                                <p className="mt-2 whitespace-pre-line text-base leading-relaxed">{note.body}</p>
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
