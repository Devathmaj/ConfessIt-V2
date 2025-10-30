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
  deleteAdminLoveNote,
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
import { ImageIcon, User, ShieldCheck, ShieldAlert, Trash2 } from 'lucide-react';

interface PersonDetails {
  id: string | null;
  name: string | null;
  regno: string | null;
  email: string | null;
}

interface AdminLoveNote {
  id: string;
  sender: PersonDetails;
  recipient: PersonDetails;
  image_url: string | null;
  message_text: string;
  is_anonymous: boolean;
  status: 'approved' | 'rejected' | 'pending_review';
  created_at: string | null;
  read_at: string | null;
}

const formatTimestamp = (value: string | null) => {
  if (!value) return 'Unknown';
  const date = new Date(value);
  return date.toLocaleString();
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

  useEffect(() => {
    const fetchNotes = async () => {
      try {
        setLoading(true);
        const response = await getAdminLoveNotes();
        const normalized = Array.isArray(response)
          ? response.map((item: Partial<AdminLoveNote>, index) => ({
              id: item.id ?? `love-note-${index}`,
              sender: {
                id: item.sender?.id ?? null,
                name: item.sender?.name ?? null,
                regno: item.sender?.regno ?? null,
                email: item.sender?.email ?? null,
              },
              recipient: {
                id: item.recipient?.id ?? null,
                name: item.recipient?.name ?? null,
                regno: item.recipient?.regno ?? null,
                email: item.recipient?.email ?? null,
              },
              image_url: item.image_url ?? null,
              message_text: item.message_text ?? '',
              is_anonymous: item.is_anonymous ?? false,
              status: (item.status as AdminLoveNote['status']) ?? 'pending_review',
              created_at: item.created_at ?? null,
              read_at: item.read_at ?? null,
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

  const filteredNotes = useMemo(() => {
    if (statusFilter === 'all') {
      return notes;
    }
    return notes.filter((note) => note.status === statusFilter);
  }, [notes, statusFilter]);

  const handleStatusChange = async (noteId: string, status: AdminLoveNote['status']) => {
    try {
      await updateAdminLoveNoteStatus(noteId, status);
      setNotes((prev) => prev.map((item) => (item.id === noteId ? { ...item, status } : item)));
      toast.success(`Love note ${status === 'approved' ? 'approved' : status === 'rejected' ? 'rejected' : 'moved to pending'}.`);
    } catch (error) {
      console.error('Failed to update love note', error);
      toast.error('Unable to update this love note.');
    }
  };

  const askDelete = (noteId: string) => {
    setSelectedNoteId(noteId);
    setDialogOpen(true);
  };

  const confirmDelete = async () => {
    if (!selectedNoteId) return;
    try {
      await deleteAdminLoveNote(selectedNoteId);
      setNotes((prev) => prev.filter((item) => item.id !== selectedNoteId));
      toast.success('Love note deleted.');
    } catch (error) {
      console.error('Failed to delete love note', error);
      toast.error('Unable to delete this love note.');
    } finally {
      setDialogOpen(false);
      setSelectedNoteId(null);
    }
  };

  const renderPerson = (person: PersonDetails, label: string) => (
    <div className="rounded-2xl border border-border bg-muted/30 p-4">
      <div className="mb-2 flex items-center gap-2 text-sm font-medium">
        <User className="h-4 w-4 text-muted-foreground" />
        {label}
      </div>
      <div className="grid gap-1 text-xs sm:text-sm">
        <p>
          <span className="text-muted-foreground">Name:</span> {person.name ?? 'Unknown'}
        </p>
        <p>
          <span className="text-muted-foreground">Reg No:</span> {person.regno ?? 'Unknown'}
        </p>
        <p>
          <span className="text-muted-foreground">Email:</span> {person.email ?? 'Unknown'}
        </p>
        <p>
          <span className="text-muted-foreground">User ID:</span> {person.id ?? 'Unknown'}
        </p>
      </div>
    </div>
  );

  return (
    <div className="relative min-h-screen bg-background">
      <AdminNavigation />

      <div className="mx-auto flex w-full max-w-6xl flex-col gap-8 px-4 pb-16 pt-28">
        <header className="flex flex-col gap-2">
          <h1 className="text-3xl font-semibold">Love Notes Review</h1>
          <p className="text-sm text-muted-foreground">
            Moderate every love note with full sender and recipient visibility.
          </p>
          <p className="text-xs text-muted-foreground/80">
            Total notes: {notes.length} · Pending decisions: {pendingCount}
          </p>
        </header>

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
          <Badge variant="secondary">
            Showing {filteredNotes.length} of {notes.length}
          </Badge>
        </div>

        {loading ? (
          <Card>
            <CardContent className="py-12 text-center text-sm text-muted-foreground">
              Loading love notes for review...
            </CardContent>
          </Card>
        ) : filteredNotes.length === 0 ? (
          <Card>
            <CardContent className="py-12 text-center text-sm text-muted-foreground">
              No love notes in this state.
            </CardContent>
          </Card>
        ) : (
          <Accordion type="single" collapsible className="space-y-3">
            {filteredNotes.map((note) => {
              const currentStatus = statusConfig[note.status];
              const previewText = note.message_text.length > 90
                ? `${note.message_text.slice(0, 90)}...`
                : note.message_text;
              return (
                <AccordionItem
                  key={note.id}
                  value={note.id}
                  className="overflow-hidden rounded-2xl border border-border bg-muted/20"
                >
                  <AccordionTrigger className="flex w-full flex-wrap items-start justify-between gap-3 px-4 py-3 text-left">
                    <div className="flex flex-col gap-1">
                      <span className="text-sm font-semibold">
                        To {note.recipient.name ?? note.recipient.regno ?? 'Unknown recipient'}
                      </span>
                      <span className="text-xs text-muted-foreground">
                        Created {formatTimestamp(note.created_at)} · {previewText || 'No message body provided.'}
                      </span>
                    </div>
                    <Badge variant={currentStatus.variant}>{currentStatus.label}</Badge>
                  </AccordionTrigger>
                  <AccordionContent className="space-y-5 bg-background/40 px-4 pb-5 pt-2">
                    <div className="grid gap-4 md:grid-cols-2">
                      {renderPerson(note.sender, 'Sender details')}
                      {renderPerson(note.recipient, 'Recipient details')}
                    </div>

                    <div className="rounded-2xl border border-border bg-muted/20 p-4">
                      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
                        <ImageIcon className="h-4 w-4 text-muted-foreground" />
                        Attachments
                      </div>
                      {note.image_url ? (
                        <img
                          src={note.image_url}
                          alt="Love note illustration"
                          className="max-h-64 w-full rounded-xl object-cover"
                        />
                      ) : (
                        <p className="text-xs text-muted-foreground">No image uploaded with this note.</p>
                      )}
                    </div>

                    <div className="rounded-2xl border border-border bg-background/60 p-4">
                      <p className="text-sm uppercase tracking-wide text-muted-foreground">Message</p>
                      <p className="mt-2 whitespace-pre-line text-base leading-relaxed">{note.message_text}</p>
                    </div>

                    <div className="grid gap-3 text-xs text-muted-foreground sm:grid-cols-2">
                      <div className="rounded-2xl border border-border bg-muted/20 p-4">
                        <p className="font-medium text-foreground/80">Visibility</p>
                        <p>{note.is_anonymous ? 'Anonymous to recipient' : 'Sender details revealed to recipient'}</p>
                      </div>
                      <div className="rounded-2xl border border-border bg-muted/20 p-4">
                        <p className="font-medium text-foreground/80">Read status</p>
                        <p>Delivered at: {formatTimestamp(note.read_at)}</p>
                      </div>
                    </div>

                    <div className="flex flex-wrap items-center justify-end gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        className="border-muted-foreground text-muted-foreground hover:bg-muted"
                        onClick={() => handleStatusChange(note.id, 'pending_review')}
                      >
                        <ShieldAlert className="mr-2 h-4 w-4" /> Mark pending
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="border-green-500 text-green-500 hover:bg-green-500 hover:text-white"
                        onClick={() => handleStatusChange(note.id, 'approved')}
                      >
                        <ShieldCheck className="mr-2 h-4 w-4" /> Approve
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground"
                        onClick={() => handleStatusChange(note.id, 'rejected')}
                      >
                        <ShieldAlert className="mr-2 h-4 w-4" /> Reject
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground"
                        onClick={() => askDelete(note.id)}
                      >
                        <Trash2 className="mr-2 h-4 w-4" /> Delete note
                      </Button>
                    </div>
                  </AccordionContent>
                </AccordionItem>
              );
            })}
          </Accordion>
        )}
      </div>

      <AlertDialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this love note?</AlertDialogTitle>
            <AlertDialogDescription>
              This will remove the note permanently for both sender and recipient.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmDelete} className="bg-destructive hover:bg-destructive/90">
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
};
