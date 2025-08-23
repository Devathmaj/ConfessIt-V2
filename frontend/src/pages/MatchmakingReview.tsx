
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Check, X, ArrowLeft } from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';
import { Badge } from '@/components/ui/badge';

// Mock data for matchmaking requests
const mockMatchmakingRequests = [
  {
    id: '1',
    user_id: 'user20',
    timestamp: new Date().toISOString(),
    status: 'pending',
    details: {
      interests: ['Gaming', 'Music', 'Movies'],
      looking_for: 'Someone to watch movies with.',
    }
  },
  {
    id: '2',
    user_id: 'user21',
    timestamp: new Date().toISOString(),
    status: 'approved',
    details: {
      interests: ['Sports', 'Reading', 'Hiking'],
      looking_for: 'A hiking partner.',
    }
  },
  {
    id: '3',
    user_id: 'user22',
    timestamp: new Date().toISOString(),
    status: 'rejected',
    details: {
      interests: ['Art', 'Cooking', 'Photography'],
      looking_for: 'Someone to explore art galleries with.',
    }
  },
];

export const MatchmakingReview = () => {
  const [requests, setRequests] = useState(mockMatchmakingRequests);

  const handleApprove = (id: string) => {
    setRequests(requests.map(r => r.id === id ? { ...r, status: 'approved' } : r));
    toast.success('Matchmaking request approved!');
  };

  const handleReject = (id: string) => {
    setRequests(requests.map(r => r.id === id ? { ...r, status: 'rejected' } : r));
    toast.error('Matchmaking request rejected!');
  };

  return (
    <div className="min-h-screen p-4 pt-24">
      <Navigation />
      <div className="max-w-4xl mx-auto">
        <div className="flex items-center gap-4 mb-6">
          <Button variant="outline" size="sm" onClick={() => window.history.back()}>
            <ArrowLeft className="w-4 h-4 mr-2" />
            Back
          </Button>
        </div>
        <div className="text-center mb-8">
          <h1 className="text-5xl font-dancing text-romantic mb-4">
            Matchmaking Review
          </h1>
          <p className="text-xl text-muted-foreground">
            Review and approve matchmaking requests.
          </p>
        </div>

        <div className="space-y-6">
          {requests.map((request) => (
            <Card key={request.id} className="confession-card">
              <CardHeader>
                <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="text-lg font-dancing text-romantic">
                      User: {request.user_id}
                    </CardTitle>
                    <CardDescription>{new Date(request.timestamp).toLocaleString()}</CardDescription>
                  </div>
                  <Badge variant={
                    request.status === 'pending' ? 'secondary' :
                    request.status === 'approved' ? 'default' :
                    'destructive'
                  }>
                    {request.status}
                  </Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <div>
                  <h4 className="font-semibold">Interests:</h4>
                  <div className="flex flex-wrap gap-2 mt-1">
                    {request.details.interests.map(interest => (
                      <Badge key={interest} variant="outline">{interest}</Badge>
                    ))}
                  </div>
                </div>
                <div>
                  <h4 className="font-semibold">Looking for:</h4>
                  <p className="text-muted-foreground">{request.details.looking_for}</p>
                </div>
                {request.status === 'pending' && (
                  <div className="flex items-center justify-end gap-2 pt-4 border-t border-border">
                    <Button variant="outline" size="sm" onClick={() => handleApprove(request.id)} className="text-green-500 border-green-500 hover:bg-green-500 hover:text-white">
                      <Check className="w-4 h-4 mr-2" />
                      Approve
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => handleReject(request.id)} className="text-red-500 border-red-500 hover:bg-red-500 hover:text-white">
                      <X className="w-4 h-4 mr-2" />
                      Reject
                    </Button>
                  </div>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    </div>
  );
};
