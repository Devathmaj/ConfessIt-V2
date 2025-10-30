
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { AdminNavigation } from '@/components/AdminNavigation';
import { getAdminMatchmaking, updateAdminMatchmakingStatus } from '@/services/api';
import { Check, X, ArrowLeft, SlidersHorizontal, RefreshCcw, Clock, Users } from 'lucide-react';
import { toast } from 'sonner';

type MatchmakingStatus = 'pending' | 'approved' | 'rejected' | 'expired';

interface MatchParticipant {
  id?: string;
  Name?: string | null;
  Regno?: string | null;
  username?: string | null;
  which_class?: string | null;
  email?: string | null;
  user_role?: string | null;
}

interface MatchmakingItem {
  id: string;
  created_at?: string | null;
  expires_at?: string | null;
  expired: boolean;
  status: MatchmakingStatus;
  participants: MatchParticipant[];
  conversation?: {
    id?: string;
    status?: string;
    requested_at?: string | null;
    accepted_at?: string | null;
    initiator?: string | null;
    receiver?: string | null;
  } | null;
}

export const MatchmakingReview = () => {
  const [requests, setRequests] = useState<MatchmakingItem[]>([]);
  const [statusFilter, setStatusFilter] = useState<'all' | MatchmakingStatus>('all');
  const [loading, setLoading] = useState(false);
  const [updating, setUpdating] = useState<string | null>(null);
  const navigate = useNavigate();

  const loadRequests = async () => {
    try {
      setLoading(true);
      const data: MatchmakingItem[] = await getAdminMatchmaking();
      setRequests(data);
    } catch (error) {
      console.error('Failed to load matchmaking data', error);
      toast.error('Unable to load matchmaking activity.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadRequests();
  }, []);

  const handleModeration = async (id: string, status: 'approved' | 'rejected' | 'pending') => {
    try {
      setUpdating(id);
      await updateAdminMatchmakingStatus(id, status);
      toast.success(`Match updated to ${status}.`);
      await loadRequests();
    } catch (error) {
      console.error('Failed to update matchmaking status', error);
      toast.error('Unable to update match status.');
    } finally {
      setUpdating(null);
    }
  };

  const filteredRequests = useMemo(() => {
    if (statusFilter === 'all') return requests;
    return requests.filter((request) => request.status === statusFilter);
  }, [requests, statusFilter]);

  const pendingCount = useMemo(
    () => requests.filter((request) => request.status === 'pending').length,
    [requests]
  );

  return (
    <div className="relative min-h-screen bg-background">
      <AdminNavigation />

      <div className="mx-auto flex w-full max-w-6xl flex-col gap-8 px-4 pb-16 pt-28">
        <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-3xl font-semibold">Matchmaking Review</h1>
            <p className="text-sm text-muted-foreground">
              Inspect active matches and conversations. Administrator accounts never appear in these results.
            </p>
            <p className="text-xs text-muted-foreground/80">
              Pending moderation: {pendingCount} · Total matches: {requests.length}
            </p>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => navigate(-1)}>
              <ArrowLeft className="mr-2 h-4 w-4" />
              Back
            </Button>
            <Button variant="secondary" size="sm" onClick={loadRequests} disabled={loading}>
              <RefreshCcw className="mr-2 h-4 w-4" />
              Refresh
            </Button>
          </div>
        </header>

        <section className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-2">
            <SlidersHorizontal className="h-4 w-4 text-muted-foreground" />
            <span className="text-xs font-medium uppercase text-muted-foreground">Status filter</span>
            <Select value={statusFilter} onValueChange={(value) => setStatusFilter(value as 'all' | MatchmakingStatus)}>
              <SelectTrigger className="w-44">
                <SelectValue placeholder="Status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All matches</SelectItem>
                <SelectItem value="pending">Pending</SelectItem>
                <SelectItem value="approved">Approved</SelectItem>
                <SelectItem value="rejected">Rejected</SelectItem>
                <SelectItem value="expired">Expired</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <Badge variant="secondary" className="w-max gap-1">
            <Users className="h-3.5 w-3.5" />
            Showing {filteredRequests.length} match(es)
          </Badge>
        </section>

        {loading ? (
          <Card>
            <CardContent className="py-16 text-center text-sm text-muted-foreground">
              Loading matchmaking activity...
            </CardContent>
          </Card>
        ) : filteredRequests.length === 0 ? (
          <Card>
            <CardContent className="py-16 text-center text-sm text-muted-foreground">
              No matches in this state.
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-6">
            {filteredRequests.map((request) => (
              <Card key={request.id} className="border border-border bg-muted/20">
                <CardHeader>
                  <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                    <div className="space-y-1">
                      <CardTitle className="text-lg">Match {request.id}</CardTitle>
                      <CardDescription className="flex flex-wrap items-center gap-3 text-xs">
                        {request.created_at && (
                          <span className="flex items-center gap-1">
                            <Clock className="h-3.5 w-3.5" /> Created {new Date(request.created_at).toLocaleString()}
                          </span>
                        )}
                        {request.expires_at && (
                          <span className="flex items-center gap-1">
                            <Clock className="h-3.5 w-3.5" /> Expires {new Date(request.expires_at).toLocaleString()}
                          </span>
                        )}
                      </CardDescription>
                    </div>
                    <Badge
                      variant={
                        request.status === 'pending'
                          ? 'secondary'
                          : request.status === 'approved'
                          ? 'default'
                          : request.status === 'expired'
                          ? 'outline'
                          : 'destructive'
                      }
                      className="capitalize"
                    >
                      {request.status}
                    </Badge>
                  </div>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div>
                    <h4 className="text-sm font-semibold">Participants</h4>
                    <div className="mt-2 grid gap-3 md:grid-cols-2">
                      {request.participants.map((participant, index) => (
                        <div key={`${request.id}-${index}`} className="rounded-lg border border-border p-3 text-sm">
                          <p className="font-medium">{participant.Name ?? participant.username ?? participant.Regno ?? 'Unknown user'}</p>
                          <div className="mt-1 text-xs text-muted-foreground">
                            {participant.Regno && <p>Regno: {participant.Regno}</p>}
                            {participant.which_class && <p>Class: {participant.which_class}</p>}
                            {participant.email && <p>Email: {participant.email}</p>}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  {request.conversation ? (
                    <div>
                      <h4 className="text-sm font-semibold">Conversation</h4>
                      <div className="mt-2 rounded-lg border border-border p-3 text-xs text-muted-foreground">
                        <p>Status: {request.conversation.status}</p>
                        {request.conversation.requested_at && <p>Requested: {new Date(request.conversation.requested_at).toLocaleString()}</p>}
                        {request.conversation.accepted_at && <p>Accepted: {new Date(request.conversation.accepted_at).toLocaleString()}</p>}
                        <p>Initiator: {request.conversation.initiator ?? 'Unknown'}</p>
                        <p>Receiver: {request.conversation.receiver ?? 'Unknown'}</p>
                      </div>
                    </div>
                  ) : (
                    <div className="text-xs text-muted-foreground">No conversation request yet.</div>
                  )}

                  {request.status === 'pending' && !request.expired && (
                    <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border pt-4">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleModeration(request.id, 'approved')}
                        disabled={updating === request.id}
                        className="border-green-500 text-green-500 hover:bg-green-500 hover:text-white"
                      >
                        <Check className="mr-2 h-4 w-4" /> Approve
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleModeration(request.id, 'rejected')}
                        disabled={updating === request.id}
                        className="border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground"
                      >
                        <X className="mr-2 h-4 w-4" /> Reject
                      </Button>
                    </div>
                  )}
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
