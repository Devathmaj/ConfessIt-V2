
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Check, X, ArrowLeft } from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';
import { Badge } from '@/components/ui/badge';

// Mock data for confessions
const mockConfessions = [
  {
    id: '1',
    confession: 'I have a huge crush on someone in my class but I am too shy to confess.',
    is_anonymous: true,
    user_id: 'user1',
    timestamp: new Date().toISOString(),
    status: 'pending',
  },
  {
    id: '2',
    confession: 'I accidentally broke a beaker in the chemistry lab and never told anyone.',
    is_anonymous: false,
    user_id: 'user2',
    timestamp: new Date().toISOString(),
    status: 'pending',
  },
  {
    id: '3',
    confession: 'I think the new feature in the app is not user-friendly.',
    is_anonymous: true,
    user_id: 'user3',
    timestamp: new Date().toISOString(),
    status: 'approved',
  },
    {
    id: '4',
    confession: 'I am secretly a cat person, but all my friends are dog people.',
    is_anonymous: true,
    user_id: 'user4',
    timestamp: new Date().toISOString(),
    status: 'pending',
  },
  {
    id: '5',
    confession: 'I ate the last slice of pizza and blamed it on my roommate.',
    is_anonymous: false,
    user_id: 'user5',
    timestamp: new Date().toISOString(),
    status: 'rejected',
  },
];

export const ConfessionsAdmin = () => {
  const [confessions, setConfessions] = useState(mockConfessions);

  const handleApprove = (id: string) => {
    setConfessions(confessions.map(c => c.id === id ? { ...c, status: 'approved' } : c));
    toast.success('Confession approved!');
  };

  const handleReject = (id: string) => {
    setConfessions(confessions.map(c => c.id === id ? { ...c, status: 'rejected' } : c));
    toast.error('Confession rejected!');
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
            Confession Review
          </h1>
          <p className="text-xl text-muted-foreground">
            Approve or reject user confessions.
          </p>
        </div>

        <div className="space-y-6">
          {confessions.map((confession) => (
            <Card key={confession.id} className="confession-card">
              <CardHeader>
                <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="text-lg font-dancing text-romantic">
                      {confession.is_anonymous ? "🤫 Anonymous" : `User: ${confession.user_id}`}
                    </CardTitle>
                    <CardDescription>{new Date(confession.timestamp).toLocaleString()}</CardDescription>
                  </div>
                  <Badge variant={
                    confession.status === 'pending' ? 'secondary' :
                    confession.status === 'approved' ? 'default' :
                    'destructive'
                  }>
                    {confession.status}
                  </Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <p className="text-foreground leading-relaxed">{confession.confession}</p>
                {confession.status === 'pending' && (
                  <div className="flex items-center justify-end gap-2 pt-4 border-t border-border">
                    <Button variant="outline" size="sm" onClick={() => handleApprove(confession.id)} className="text-green-500 border-green-500 hover:bg-green-500 hover:text-white">
                      <Check className="w-4 h-4 mr-2" />
                      Approve
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => handleReject(confession.id)} className="text-red-500 border-red-500 hover:bg-red-500 hover:text-white">
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
