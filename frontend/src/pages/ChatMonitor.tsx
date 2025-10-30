import { useEffect, useMemo, useState } from 'react';
import { AdminNavigation } from '@/components/AdminNavigation';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
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
import { Textarea } from '@/components/ui/textarea';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { toast } from 'sonner';
import {
  getAdminConversationDetail,
  getAdminConversations,
  terminateAdminConversation,
} from '@/services/api';
import {
  Loader2,
  MessageSquare,
  MessageSquareOff,
  ShieldAlert,
} from 'lucide-react';

interface AdminConversationParticipant {
  id?: string | null;
  Regno?: string | null;
  Name?: string | null;
  email?: string | null;
  username?: string | null;
  user_role?: string | null;
}

interface AdminConversationMatch {
  id: string;
  user_1_regno?: string | null;
  user_2_regno?: string | null;
  created_at?: string | null;
  expires_at?: string | null;
}

interface AdminConversationMessage {
  id: string;
  sender_id?: string | null;
  text?: string | null;
  created_at?: string | null;
}

interface AdminConversationSummary {
  id: string;
  status?: string | null;
  created_at?: string | null;
  requested_at?: string | null;
  accepted_at?: string | null;
  terminated_at?: string | null;
  initiator: AdminConversationParticipant;
  receiver: AdminConversationParticipant;
  match?: AdminConversationMatch | null;
  supabase_conversation_id?: string | null;
  latest_message?: AdminConversationMessage | null;
  is_blocked?: boolean;
  blocked_by?: string | null;
}

interface AdminConversationDetail extends AdminConversationSummary {
  messages: AdminConversationMessage[];
}

const statusLabels: Record<string, { label: string; variant: 'default' | 'secondary' | 'destructive' | 'outline' }> = {
  pending: { label: 'Pending', variant: 'secondary' },
  requested: { label: 'Requested', variant: 'secondary' },
  approved: { label: 'Active', variant: 'default' },
  accepted: { label: 'Accepted', variant: 'default' },
  rejected: { label: 'Rejected', variant: 'outline' },
  terminated: { label: 'Terminated', variant: 'destructive' },
};

const formatTimestamp = (value?: string | null) => {
  if (!value) {
    return 'Not available';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
};

const resolveDisplayName = (participant?: AdminConversationParticipant) => {
  if (!participant) {
    return 'Unknown';
  }
  return participant.Name || participant.username || participant.Regno || 'Unknown';
};

const truncate = (value?: string | null, length = 80) => {
  if (!value) {
    return '';
  }
  if (value.length <= length) {
    return value;
  }
  return `${value.slice(0, length)}...`;
};

const ChatMonitor = () => {
  const [conversations, setConversations] = useState<AdminConversationSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedConversation, setSelectedConversation] = useState<AdminConversationDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [terminationReason, setTerminationReason] = useState('');
  const [showTerminateDialog, setShowTerminateDialog] = useState(false);

  const statusOptions = useMemo(() => {
    const values = new Set<string>();
    conversations.forEach((item) => {
      if (item.status) {
        values.add(item.status);
      }
    });
    return Array.from(values).sort();
  }, [conversations]);

  useEffect(() => {
    const fetchConversations = async () => {
      try {
        setLoading(true);
        const data = await getAdminConversations();
        setConversations(data);
        if (data.length) {
          setSelectedId(data[0].id);
        }
      } catch (error) {
        console.error('Failed to load conversations', error);
        toast.error('Unable to load conversations');
      } finally {
        setLoading(false);
      }
    };

    fetchConversations();
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setSelectedConversation(null);
      return;
    }

    const fetchDetail = async () => {
      try {
        setDetailLoading(true);
        const data = await getAdminConversationDetail(selectedId);
        setSelectedConversation(data);
      } catch (error) {
        console.error('Failed to load conversation detail', error);
        toast.error('Unable to load conversation detail');
        setSelectedConversation(null);
      } finally {
        setDetailLoading(false);
      }
    };

    fetchDetail();
  }, [selectedId]);

  const filteredConversations = useMemo(() => {
    if (statusFilter === 'all') {
      return conversations;
    }
    return conversations.filter((item) => item.status === statusFilter);
  }, [conversations, statusFilter]);

  const handleSelectConversation = (conversationId: string) => {
    setSelectedId(conversationId);
  };

  const openTerminateDialog = () => {
    setTerminationReason('');
    setShowTerminateDialog(true);
  };

  const confirmTerminate = async () => {
    if (!selectedConversation) {
      return;
    }

    try {
      await terminateAdminConversation(selectedConversation.id, {
        reason: terminationReason.trim() || undefined,
      });

      toast.success('Conversation terminated');
      setShowTerminateDialog(false);

      // Update local cache to reflect new status without refetching everything
      setConversations((prev) =>
        prev.map((item) =>
          item.id === selectedConversation.id
            ? { ...item, status: 'terminated', terminated_at: new Date().toISOString() }
            : item
        )
      );

      setSelectedConversation((prev) =>
        prev
          ? {
              ...prev,
              status: 'terminated',
              terminated_at: new Date().toISOString(),
            }
          : prev
      );
    } catch (error) {
      console.error('Failed to terminate conversation', error);
      toast.error('Unable to terminate conversation');
    }
  };

  const renderStatusBadge = (status?: string | null) => {
    if (!status) {
      return <Badge variant="secondary">Unknown</Badge>;
    }

    const config = statusLabels[status] || { label: status, variant: 'secondary' };
    return <Badge variant={config.variant}>{config.label}</Badge>;
  };

  return (
    <div className="relative min-h-screen bg-background">
      <AdminNavigation />

      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 pb-16 pt-28">
        <header className="flex flex-col gap-3">
          <div>
            <CardTitle className="text-2xl">Chat monitor</CardTitle>
            <CardDescription className="text-sm text-muted-foreground">
              Review active conversations and terminate sessions that violate community guidelines.
            </CardDescription>
          </div>

          <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
            <span className="uppercase tracking-wide">Status</span>
            <Select value={statusFilter} onValueChange={(value) => setStatusFilter(value)}>
              <SelectTrigger className="w-40">
                <SelectValue placeholder="Filter status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All conversations</SelectItem>
                {statusOptions.map((statusValue) => (
                  <SelectItem key={statusValue} value={statusValue}>
                    {statusLabels[statusValue]?.label || statusValue}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Badge variant="secondary">{filteredConversations.length} showing</Badge>
          </div>
        </header>

        {loading ? (
          <div className="flex h-[420px] items-center justify-center">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
          </div>
        ) : filteredConversations.length === 0 ? (
          <Card>
            <CardContent className="py-16 text-center text-sm text-muted-foreground">
              There are no conversations matching this filter.
            </CardContent>
          </Card>
        ) : (
          <div className="grid gap-6 lg:grid-cols-[320px_1fr]">
            <Card className="h-full">
              <CardHeader>
                <CardTitle className="text-lg">Conversations</CardTitle>
                <CardDescription>Select a conversation to view the transcript and actions.</CardDescription>
              </CardHeader>
              <CardContent className="p-0">
                <ScrollArea className="h-[600px]">
                  <div className="divide-y">
                    {filteredConversations.map((conversation) => {
                      const isActive = selectedId === conversation.id;
                      const previewText = truncate(conversation.latest_message?.text, 90) || 'No messages yet';
                      return (
                        <button
                          key={conversation.id}
                          type="button"
                          onClick={() => handleSelectConversation(conversation.id)}
                          className={`flex w-full flex-col gap-1 px-4 py-3 text-left transition hover:bg-muted ${
                            isActive ? 'bg-muted' : ''
                          }`}
                        >
                          <div className="flex items-center justify-between gap-2 text-sm font-medium">
                            <span>
                              {resolveDisplayName(conversation.initiator)} ↔ {resolveDisplayName(conversation.receiver)}
                            </span>
                            {renderStatusBadge(conversation.status)}
                          </div>
                          <p className="text-xs text-muted-foreground">{previewText}</p>
                          <p className="text-[11px] uppercase tracking-wide text-muted-foreground">
                            Updated {formatTimestamp(conversation.latest_message?.created_at || conversation.accepted_at || conversation.created_at)}
                          </p>
                        </button>
                      );
                    })}
                  </div>
                </ScrollArea>
              </CardContent>
            </Card>

            <Card className="h-full">
              <CardHeader>
                <CardTitle className="text-lg">Conversation detail</CardTitle>
                <CardDescription>
                  {selectedConversation
                    ? `${resolveDisplayName(selectedConversation.initiator)} and ${resolveDisplayName(selectedConversation.receiver)}`
                    : 'Select a conversation to view messages.'}
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">
                {detailLoading ? (
                  <div className="flex h-64 items-center justify-center">
                    <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                  </div>
                ) : !selectedConversation ? (
                  <div className="rounded-2xl border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
                    Choose a conversation from the list to inspect messages and moderation actions.
                  </div>
                ) : (
                  <>
                    <div className="grid gap-4 text-sm text-muted-foreground sm:grid-cols-2">
                      <div className="rounded-2xl border border-border bg-muted/20 p-4">
                        <p className="text-xs uppercase tracking-wide">Status</p>
                        <div className="mt-2 flex items-center gap-2 text-sm text-foreground">
                          {renderStatusBadge(selectedConversation.status)}
                          {selectedConversation.is_blocked && (
                            <Badge variant="destructive">
                              <span className="flex items-center gap-1">
                                <ShieldAlert className="h-3 w-3" /> Blocked in chat
                              </span>
                            </Badge>
                          )}
                        </div>
                        {selectedConversation.blocked_by && (
                          <p className="mt-2 text-xs">Blocked by: {selectedConversation.blocked_by}</p>
                        )}
                      </div>
                      <div className="rounded-2xl border border-border bg-muted/20 p-4">
                        <p className="text-xs uppercase tracking-wide">Timeline</p>
                        <ul className="mt-2 space-y-1 text-xs">
                          <li>Requested: {formatTimestamp(selectedConversation.requested_at)}</li>
                          <li>Accepted: {formatTimestamp(selectedConversation.accepted_at)}</li>
                          <li>Terminated: {formatTimestamp(selectedConversation.terminated_at)}</li>
                        </ul>
                      </div>
                    </div>

                    <div>
                      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                        Transcript
                      </h3>
                      <div className="rounded-2xl border border-border bg-background/60">
                        <ScrollArea className="h-[360px] px-4 py-4">
                          <div className="flex flex-col gap-3">
                            {selectedConversation.messages.length === 0 ? (
                              <div className="rounded-xl border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
                                No messages exchanged yet.
                              </div>
                            ) : (
                              selectedConversation.messages.map((message) => (
                                <div
                                  key={message.id}
                                  className="rounded-xl border border-border bg-muted/20 px-4 py-3 text-sm"
                                >
                                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                                    <MessageSquare className="h-3.5 w-3.5" />
                                    <span>{message.sender_id || 'Unknown sender'}</span>
                                    <span>•</span>
                                    <span>{formatTimestamp(message.created_at)}</span>
                                  </div>
                                  <p className="mt-2 whitespace-pre-wrap text-sm text-foreground">
                                    {message.text || 'No content'}
                                  </p>
                                </div>
                              ))
                            )}
                          </div>
                        </ScrollArea>
                      </div>
                    </div>

                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <p className="text-xs text-muted-foreground">
                        Terminating a conversation will immediately block both participants from messaging in this match.
                      </p>
                      <Button
                        variant="destructive"
                        size="sm"
                        disabled={selectedConversation.status === 'terminated'}
                        onClick={openTerminateDialog}
                        className="flex items-center gap-2"
                      >
                        <MessageSquareOff className="h-4 w-4" />
                        Terminate conversation
                      </Button>
                    </div>
                  </>
                )}
              </CardContent>
            </Card>
          </div>
        )}
      </div>

      <AlertDialog open={showTerminateDialog} onOpenChange={setShowTerminateDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Terminate conversation?</AlertDialogTitle>
            <AlertDialogDescription>
              This action cannot be undone. Participants will immediately lose access to the chat. Optionally provide a reason to store alongside the termination record.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-2">
            <p className="text-xs text-muted-foreground">Reason (optional)</p>
            <Textarea
              value={terminationReason}
              onChange={(event) => setTerminationReason(event.target.value)}
              placeholder="Inappropriate content, harassment, spam, etc."
              rows={3}
            />
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmTerminate} className="bg-destructive text-destructive-foreground hover:bg-destructive/90">
              Terminate
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
};

export default ChatMonitor;
