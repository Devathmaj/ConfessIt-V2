
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Check, X, ArrowLeft } from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';
import { Badge } from '@/components/ui/badge';

// Mock data for love notes
const mockLoveNotes = [
  {
    id: '1',
    recipient_id: 'user10',
    message_text: 'Your smile brightens my day. Would love to get to know you better.',
    is_anonymous: true,
    sender_id: 'user1',
    timestamp: new Date().toISOString(),
    status: 'pending',
    image_url: 'https://placehold.co/300x200/fecdd3/be123c?text=Love+Note',
  },
  {
    id: '2',
    recipient_id: 'user12',
    message_text: 'I have admired you from afar for a long time. You seem like a wonderful person.',
    is_anonymous: false,
    sender_id: 'user2',
    timestamp: new Date().toISOString(),
    status: 'pending',
    image_url: 'https://placehold.co/300x200/fecdd3/be123c?text=For+You',
  },
  {
    id: '3',
    recipient_id: 'user15',
    message_text: 'Happy Valentine\'s Day!',
    is_anonymous: true,
    sender_id: 'user3',
    timestamp: new Date().toISOString(),
    status: 'approved',
    image_url: 'https://placehold.co/300x200/fecdd3/be123c?text=Approved',
  },
];

export const LoveNotesReview = () => {
  const [loveNotes, setLoveNotes] = useState(mockLoveNotes);

  const handleApprove = (id: string) => {
    setLoveNotes(loveNotes.map(ln => ln.id === id ? { ...ln, status: 'approved' } : ln));
    toast.success('Love Note approved!');
  };

  const handleReject = (id: string) => {
    setLoveNotes(loveNotes.map(ln => ln.id === id ? { ...ln, status: 'rejected' } : ln));
    toast.error('Love Note rejected!');
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
            Love Notes Review
          </h1>
          <p className="text-xl text-muted-foreground">
            Review and approve love notes sent between users.
          </p>
        </div>

        <div className="space-y-6">
          {loveNotes.map((note) => (
            <Card key={note.id} className="confession-card">
              <CardHeader>
                <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="text-lg font-dancing text-romantic">
                      To: {note.recipient_id}
                    </CardTitle>
                    <CardDescription>
                      From: {note.is_anonymous ? "🤫 Anonymous" : note.sender_id}
                    </CardDescription>
                  </div>
                  <Badge variant={
                    note.status === 'pending' ? 'secondary' :
                    note.status === 'approved' ? 'default' :
                    'destructive'
                  }>
                    {note.status}
                  </Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <img src={note.image_url} alt="Love note" className="rounded-lg" />
                <p className="text-foreground leading-relaxed">{note.message_text}</p>
                {note.status === 'pending' && (
                  <div className="flex items-center justify-end gap-2 pt-4 border-t border-border">
                    <Button variant="outline" size="sm" onClick={() => handleApprove(note.id)} className="text-green-500 border-green-500 hover:bg-green-500 hover:text-white">
                      <Check className="w-4 h-4 mr-2" />
                      Approve
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => handleReject(note.id)} className="text-red-500 border-red-500 hover:bg-red-500 hover:text-white">
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
