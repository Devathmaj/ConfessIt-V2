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
  const { user } = useAuth();
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

  /**
   * The ids of the turns on screen, kept in step with the list synchronously.
   *
   * The list is state, and a state update only becomes visible after the next
   * render, so a de-dupe that reads the list sees a turn as absent while it is
   * already on screen. That is how the count came to be wrong: the local append
   * added one and the live echo of the same turn added another, because the
   * echo's de-dupe guarded the row and not the counter.
   */
  const messageIdsRef = useRef<Set<string>>(new Set());

  /**
   * Put a turn on screen, unless it is already there.
   *
   * Answers whether it was added, so the caller knows whether the count and the
   * allowance move. The count is the number of turns the thread holds, and a turn
   * spends the room -- so a turn that was already on screen must move neither.
   */
  const appendMessage = (message: CaseClosedChatMessage): boolean => {
    if (messageIdsRef.current.has(message.id)) {
      return false;
    }
    messageIdsRef.current.add(message.id);
    setMessages((previous) => [...previous, message]);
    setTotal((t) => t + 1);
    return true;
  };

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
          if (!before) {
            // The list is replaced, so the known ids are replaced with it.
            messageIdsRef.current = new Set(page.messages.map((message) => message.id));
            return page.messages;
          }
          // The older rows are added to the list, so their ids join the known ones.
          page.messages.forEach((message) => messageIdsRef.current.add(message.id));
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
    //
    // The bearer is deliberately not part of this: it rotates every time the
    // server refuses an expired one, and a rotation must not tear down a live
    // thread and open it again. The ticket exchange reads the bearer that is
    // current when it happens, and a thread opened without a session is refused
    // by the server like any other.
    if (!sessionId || !threadOpen || subscribedRef.current) return;
    subscribedRef.current = true;
    let offMessage: (() => void) | null = null;
    let offError: (() => void) | null = null;
    (async () => {
      try {
        const opened = await caseClosedMessagingClient.connect();
        if (!opened) {
          // Overtaken: the thread closed while the ticket was being fetched.
          // There is no socket to subscribe to, and none to leave behind.
          return;
        }
        setConnected(true);
        offMessage = caseClosedMessagingClient.onMessage((incoming) => {
          // The turn is added once, and the count and the allowance move with it.
          // The sender's own turn is added by the send handler below, and the
          // echo of it lands here: it is already on screen, so neither the count
          // nor the allowance may move again -- the allowance was already spent
          // by the send, which is why the echo must not spend it a second time.
          const added = appendMessage({
            ...incoming,
            is_sender: incoming.sender_user_id === user?.id,
          });
          if (added) {
            setQuota((q) => quotaAfterIncomingMessage(q, incoming.sender_user_id === user?.id));
            scrollToBottom();
          }
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
  }, [sessionId, threadOpen, user?.id, scrollToBottom]);

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
          <span className="mt-1 block" role="note">
            For your privacy, Case Closed chat messages are automatically deleted when this game
            session expires.
          </span>
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
                    // The turn goes on screen here, and the count moves with it.
                    // The live echo of the same turn lands on the socket handler
                    // above, which finds it already here and moves neither the
                    // count nor the allowance -- the allowance came from the
                    // server's answer, which already counts this turn.
                    appendMessage({ ...sent, is_sender: true });
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
