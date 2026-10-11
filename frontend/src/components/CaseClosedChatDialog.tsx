// src/components/CaseClosedChatDialog.tsx
/**
 * The Case Closed detective thread, embedded in the game window.
 *
 * Same dialogue-box shape as the matchmaking chat (thread header, transcript,
 * composer, quota, live socket) but scoped to the couple's Case Closed
 * session: the thread is found via the caller's session (game + session travel
 * together), quotas are 400 per thread / 200 per detective, and realtime rides
 * the shared Redis pub/sub on the `case-closed:{session_id}` channel.
 *
 * Messaging never consumes an investigation action; the REST surface is the
 * case-closed router (`services/api`), the socket is `lib/caseClosedMessaging`.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Flag, Loader2, MessagesSquare, RefreshCw, Send } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { useAuth } from '@/contexts/AuthContext';
import { describeApiError } from '@/lib/apiError';
import { cn } from '@/lib/utils';
import { ReportMessageDialog } from '@/components/ReportMessageDialog';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {
  caseClosedLimitNotice,
  caseClosedMessagingClient,
  caseClosedQuotaAfterRemovedMessage,
  caseClosedQuotaLabel,
  emptyCaseClosedQuota,
} from '@/lib/caseClosedMessaging';
import { quotaAfterIncomingMessage } from '@/lib/messageQuota';
import { MESSAGE_BODY_MAX } from '@/lib/limits';
import {
  getCaseClosedMessages,
  reportCaseClosedMessage,
  sendCaseClosedMessage,
  type CaseClosedChatMessage,
  type CaseClosedMessageQuota,
} from '@/services/api';

const PAGE_SIZE = 50;

export const CaseClosedChatDialog = ({
  sessionId,
  chatOpen = true,
  closedReason = null,
}: {
  sessionId: string | null;
  /**
   * The game state's view of the thread: false the moment either detective
   * confirms, so the composer and the live socket end together without waiting
   * for a transcript reload.
   */
  chatOpen?: boolean;
  /** Why the thread is closed, from the caller's game state. */
  closedReason?: string | null;
}) => {
  const { token, user } = useAuth();
  const [messages, setMessages] = useState<CaseClosedChatMessage[]>([]);
  const [threadReport, setThreadReport] = useState<{ open: boolean; reason: string | null } | null>(null);
  const [total, setTotal] = useState(0);
  const [quota, setQuota] = useState<CaseClosedMessageQuota | null>(null);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [draft, setDraft] = useState('');
  const [connected, setConnected] = useState(false);
  const [violationDialog, setViolationDialog] = useState<{ open: boolean; message: string }>({
    open: false,
    message: '',
  });
  const [reportTarget, setReportTarget] = useState<CaseClosedChatMessage | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const subscribedRef = useRef(false);

  // Both gates must agree the thread is open; either can close it alone. The
  // page knows about the lock first (its confirmation response lands there),
  // the server knows about everything (case end, event rollover, a racing
  // lock), and the reason shown prefers the server's word when it has one.
  const threadOpen = chatOpen && (threadReport?.open ?? true);
  const closedNote = threadOpen
    ? null
    : threadReport?.reason ?? closedReason ?? 'The detective thread is closed';

  const scrollToBottom = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, []);

  const load = useCallback(
    async (before?: string) => {
      if (!before) setLoading(true);
      try {
        const page = await getCaseClosedMessages(PAGE_SIZE, before);
        setQuota(page.quota ?? emptyCaseClosedQuota());
        setTotal(page.total);
        // The server's word is the backstop: if a partner locked between the
        // caller's state polls, this transcript read still closes the thread.
        setThreadReport({ open: page.chat_open, reason: page.closed_reason });
        setMessages((prev) => {
          if (!before) return page.messages;
          const known = new Set(prev.map((m) => m.id));
          return [...page.messages.filter((m) => !known.has(m.id)), ...prev];
        });
      } catch (error) {
        toast.error(describeApiError(error, 'Could not load the detective thread.'));
      } finally {
        setLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    if (!sessionId) return;
    void load();
  }, [sessionId, load]);

  useEffect(() => {
    // The socket exists only while the thread does: closing the chat (or the
    // case ending) re-runs this effect and the cleanup below drops the
    // connection, so nothing persistent survives the lock. The server refuses
    // the handshake outright when the thread is already closed.
    if (!sessionId || !token || !threadOpen || subscribedRef.current) return;
    subscribedRef.current = true;
    let offMessage: (() => void) | null = null;
    let offError: (() => void) | null = null;
    (async () => {
      try {
        await caseClosedMessagingClient.connect(token);
        setConnected(true);
        offMessage = caseClosedMessagingClient.onMessage((incoming) => {
          setMessages((prev) => {
            if (prev.some((m) => m.id === incoming.id)) return prev;
            return [...prev, { ...incoming, is_sender: incoming.sender_user_id === user?.id }];
          });
          setTotal((t) => t + 1);
          setQuota((q) => quotaAfterIncomingMessage(q, incoming.sender_user_id === user?.id));
          scrollToBottom();
        });
        offError = caseClosedMessagingClient.onError(() => setConnected(false));
      } catch {
        setConnected(false);
      }
    })();
    return () => {
      offMessage?.();
      offError?.();
      caseClosedMessagingClient.disconnect();
      subscribedRef.current = false;
      setConnected(false);
    };
  }, [sessionId, token, threadOpen, user?.id, scrollToBottom]);

  useEffect(() => {
    scrollToBottom();
  }, [messages.length, scrollToBottom]);

  if (!sessionId) return null;

  const canSend = quota === null || quota.can_send;
  const notice = caseClosedLimitNotice(quota);
  const remaining = total > messages.length ? total - messages.length : 0;

  return (
    <>
      <Card>
        <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <MessagesSquare className="h-4 w-4" /> Detective thread
          <Badge variant={!threadOpen ? 'outline' : connected ? 'default' : 'secondary'}>
            {!threadOpen ? 'closed' : connected ? 'live' : 'polling'}
          </Badge>
          {caseClosedQuotaLabel(quota) && <Badge variant="outline">{caseClosedQuotaLabel(quota)}</Badge>}
        </CardTitle>
        <CardDescription>
          Private to your pair for this case (400 per thread, 200 each). Chatting never costs an
          investigation action, and the thread closes the moment either of you confirms the accusation.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading the thread…
          </div>
        ) : (
          <>
            {remaining > 0 && (
              <Button
                variant="ghost"
                size="sm"
                className="w-fit"
                onClick={() => void load(messages[0]?.id)}
              >
                Show older ({remaining} more)
              </Button>
            )}
            <div className="flex max-h-80 flex-col gap-2 overflow-y-auto rounded-xl border p-3">
              {messages.length === 0 && (
                <div className="text-sm text-muted-foreground">
                  No messages yet — agree who investigates what first.
                </div>
              )}
              {messages.map((m) => {
                const mine = m.is_sender ?? m.sender_user_id === user?.id;
                return (
                  <div key={m.id} className={cn('max-w-[85%] rounded-xl px-3 py-2 text-sm', mine ? 'self-end bg-primary text-primary-foreground' : 'self-start bg-muted')}>
                    <div className="whitespace-pre-wrap break-words">{m.body}</div>
                    <div className={cn('mt-1 flex items-center gap-2 text-[11px]', mine ? 'text-primary-foreground/70' : 'text-muted-foreground')}>
                      <span>{new Date(m.created_at).toLocaleTimeString()}</span>
                      {/* Only a received turn can be reported: a sender does not report their
                          own words, and the server refuses that too. */}
                      {!mine && (
                        <button
                          type="button"
                          className="flex items-center gap-1 hover:text-destructive"
                          onClick={() => setReportTarget(m)}
                          aria-label="Report this message"
                        >
                          <Flag className="h-3 w-3" /> Report
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
              <div ref={bottomRef} />
            </div>
            {notice && <div className="text-xs text-destructive">{notice}</div>}
            {threadOpen ? (
              <form
                className="flex gap-2"
                onSubmit={async (e) => {
                  e.preventDefault();
                  const body = draft.trim();
                  if (!body || sending || !canSend) return;
                  setSending(true);
                  try {
                    const sent = await sendCaseClosedMessage(body);
                    setQuota(sent.quota);
                    setMessages((prev) => {
                      if (prev.some((m) => m.id === sent.id)) return prev;
                      return [...prev, { ...sent, is_sender: true }];
                    });
                    setTotal((t) => t + 1);
                    setDraft('');
                    scrollToBottom();
                  } catch (error) {
                    if ((error as { response?: { status?: number } }).response?.status === 422) {
                      // The turn was removed, but the attempt was spent: the
                      // server stored it and counted it, so move the counter now
                      // rather than wait for the next read.
                      setQuota((q) => caseClosedQuotaAfterRemovedMessage(q));
                      setViolationDialog({
                        open: true,
                        message: describeApiError(
                          error,
                          'Your message was removed because it violated our content policies.'
                        ),
                      });
                      return;
                    }
                    toast.error(describeApiError(error, 'Could not send the message.'));
                  } finally {
                    setSending(false);
                  }
                }}
              >
                <Input
                  placeholder="Discuss leads, alibis, who investigates next…"
                  value={draft}
                  maxLength={MESSAGE_BODY_MAX}
                  onChange={(e) => setDraft(e.target.value)}
                  disabled={!canSend || sending}
                />
                <Button type="submit" size="sm" disabled={!draft.trim() || !canSend || sending}>
                  {sending ? (
                    <><Loader2 className="h-3 w-3 animate-spin" /> Sending…</>
                  ) : (
                    <><Send className="h-3 w-3" /> Send</>
                  )}
                </Button>
                <Button type="button" variant="ghost" size="sm" onClick={() => void load()}>
                  <RefreshCw className="h-3 w-3" />
                </Button>
              </form>
            ) : (
              <div className="rounded-xl border bg-muted/40 px-3 py-3">
                <div className="text-sm font-medium">Detective thread closed</div>
                <div className="mt-1 text-xs text-muted-foreground">
                  {closedNote} — earlier messages stay above to reread.
                </div>
              </div>
            )}
          </>
        )}
      </CardContent>
      </Card>

      {/* A send content safety refused. Rendered from state rather than a toast
          so the reason stays on screen until acknowledged. */}
      <AlertDialog
        open={violationDialog.open}
        onOpenChange={(open) => setViolationDialog((prev) => ({ ...prev, open }))}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Message removed</AlertDialogTitle>
            <AlertDialogDescription>
              {violationDialog.message ||
                'Your message was removed because it violated our content policies.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogAction onClick={() => setViolationDialog({ open: false, message: '' })}>
              Understood
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* The same report dialog matchmaking chat uses; only the endpoint differs. A filed
          report changes nothing here -- a moderator rules on it from the admin queue. */}
      <ReportMessageDialog
        isOpen={reportTarget !== null}
        onClose={() => setReportTarget(null)}
        messageId={reportTarget?.id ?? ''}
        messageText={reportTarget?.body ?? ''}
        reporterId={user?.id}
        reportedUserId={reportTarget?.sender_user_id}
        conversationId={sessionId ?? undefined}
        onSubmit={(messageId, reason) => reportCaseClosedMessage(messageId, reason)}
      />
    </>
  );
};

export default CaseClosedChatDialog;
