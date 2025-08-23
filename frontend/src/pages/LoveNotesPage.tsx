import { useState } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import { CountdownTimer } from '@/components/ui/countdown-timer';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { 
  Mail, 
  Heart, 
  Palette,
  Send,
  Download,
  X,
  Sparkles,
  MessageCircle
} from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';

interface LoveNote {
  id: string;
  from: string;
  message: string;
  template: string;
  anonymous: boolean;
  timestamp: Date;
  emojis: string[];
}

const templates = [
  { id: 'classic', name: 'Classic Romance', bg: 'bg-gradient-romantic', border: 'border-romantic' },
  { id: 'spring', name: 'Spring Bloom', bg: 'bg-gradient-love', border: 'border-pink-300' },
  { id: 'passion', name: 'Passionate', bg: 'bg-gradient-passion', border: 'border-red-400' },
  { id: 'dreamy', name: 'Dreamy', bg: 'bg-gradient-hero', border: 'border-purple-300' }
];

const emojiStickers = ['💕', '💖', '💗', '💘', '💝', '💌', '🌹', '🌸', '🌺', '🦋', '✨', '💫', '⭐', '🌟', '💎', '👑'];

const mockReceivedNotes: LoveNote[] = [
  {
    id: '1',
    from: 'Secret Admirer',
    message: 'Your smile brightens my day more than sunshine ☀️',
    template: 'classic',
    anonymous: true,
    timestamp: new Date('2024-02-14T10:30:00'),
    emojis: ['💕', '🌟', '✨']
  },
  {
    id: '2',
    from: 'Alex Chen',
    message: 'Thanks for being such an amazing friend! Hope your day is filled with love 💖',
    template: 'spring',
    anonymous: false,
    timestamp: new Date('2024-02-14T14:15:00'),
    emojis: ['🌸', '💖', '🦋']
  },
  {
    id: '3',
    from: 'Coffee Shop Stranger',
    message: 'You have the most beautiful laugh! Made my morning special ☕',
    template: 'dreamy',
    anonymous: true,
    timestamp: new Date('2024-02-14T08:45:00'),
    emojis: ['☕', '💫', '😊']
  }
];

export const LoveNotesPage = () => {
  const { user } = useAuth();
  const [activeTab, setActiveTab] = useState<'create' | 'inbox'>('create');
  const [selectedTemplate, setSelectedTemplate] = useState(templates[0]);
  const [noteMessage, setNoteMessage] = useState('');
  const [recipientName, setRecipientName] = useState('');
  const [isAnonymous, setIsAnonymous] = useState(false);
  const [selectedEmojis, setSelectedEmojis] = useState<string[]>([]);
  const [selectedNote, setSelectedNote] = useState<LoveNote | null>(null);

  const toggleEmoji = (emoji: string) => {
    setSelectedEmojis(prev => 
      prev.includes(emoji) 
        ? prev.filter(e => e !== emoji)
        : [...prev, emoji]
    );
  };

  const sendNote = () => {
    if (!noteMessage.trim()) {
      toast.error('Please write a message!');
      return;
    }
    if (!isAnonymous && !recipientName.trim()) {
      toast.error('Please enter recipient name or enable anonymous mode!');
      return;
    }

    toast.success('Love note sent! 💕');
    setNoteMessage('');
    setRecipientName('');
    setSelectedEmojis([]);
  };

  const downloadNote = () => {
    toast.success('Note downloaded! 📱');
  };

  return (
    <div className="min-h-screen p-4 pt-24 relative overflow-hidden">
      <Navigation />
      <FloatingHearts />
      
      {/* Header */}
      <div className="max-w-7xl mx-auto mb-8">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-6">
          <div className="text-center lg:text-left">
            <h1 className="text-5xl font-dancing text-romantic mb-2">
              Love Notes & Cards 💌
            </h1>
            <p className="text-xl text-muted-foreground">
              Create and share beautiful digital love notes
            </p>
          </div>
          <CountdownTimer />
        </div>
      </div>

      {/* Tab Navigation */}
      <div className="max-w-7xl mx-auto mb-8">
        <div className="flex gap-4 justify-center">
          <Button
            onClick={() => setActiveTab('create')}
            variant={activeTab === 'create' ? 'default' : 'outline'}
            className="font-dancing text-lg px-8"
          >
            <Palette className="w-5 h-5 mr-2" />
            Create Note
          </Button>
          <Button
            onClick={() => setActiveTab('inbox')}
            variant={activeTab === 'inbox' ? 'default' : 'outline'}
            className="font-dancing text-lg px-8"
          >
            <Mail className="w-5 h-5 mr-2" />
            Inbox ({mockReceivedNotes.length})
          </Button>
        </div>
      </div>

      <div className="max-w-7xl mx-auto">
        {activeTab === 'create' ? (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
            {/* Card Editor */}
            <div className="space-y-6">
              <Card className="confession-card">
                <CardHeader>
                  <CardTitle className="text-2xl font-dancing text-romantic">Design Your Note</CardTitle>
                </CardHeader>
                <CardContent className="space-y-6">
                  {/* Template Selection */}
                  <div>
                    <Label className="text-romantic font-semibold mb-3 block">Choose Template</Label>
                    <div className="grid grid-cols-2 gap-3">
                      {templates.map((template) => (
                        <button
                          key={template.id}
                          onClick={() => setSelectedTemplate(template)}
                          className={`p-4 rounded-lg border-2 transition-all duration-300 hover:scale-105 ${
                            selectedTemplate.id === template.id 
                              ? `${template.border} ${template.bg}` 
                              : 'border-muted hover:border-romantic'
                          }`}
                        >
                          <div className={`w-full h-12 ${template.bg} rounded mb-2`} />
                          <p className="text-sm font-dancing text-romantic">{template.name}</p>
                        </button>
                      ))}
                    </div>
                  </div>

                  {/* Message Input */}
                  <div>
                    <Label htmlFor="message" className="text-romantic font-semibold">Your Message</Label>
                    <Textarea
                      id="message"
                      placeholder="Write your heartfelt message here..."
                      value={noteMessage}
                      onChange={(e) => setNoteMessage(e.target.value)}
                      rows={4}
                      className="resize-none"
                    />
                  </div>

                  {/* Emoji Stickers */}
                  <div>
                    <Label className="text-romantic font-semibold mb-3 block">Add Stickers</Label>
                    <div className="grid grid-cols-8 gap-2">
                      {emojiStickers.map((emoji) => (
                        <button
                          key={emoji}
                          onClick={() => toggleEmoji(emoji)}
                          className={`w-10 h-10 text-lg rounded-lg border-2 transition-all duration-300 hover:scale-110 ${
                            selectedEmojis.includes(emoji)
                              ? 'border-romantic bg-gradient-love'
                              : 'border-muted hover:border-romantic'
                          }`}
                        >
                          {emoji}
                        </button>
                      ))}
                    </div>
                  </div>

                  {/* Recipient Settings */}
                  <div className="space-y-4">
                    <div className="flex items-center justify-between">
                      <Label className="text-romantic font-semibold">Send Anonymously</Label>
                      <Switch checked={isAnonymous} onCheckedChange={setIsAnonymous} />
                    </div>
                    
                    {!isAnonymous && (
                      <div>
                        <Label htmlFor="recipient" className="text-romantic font-semibold">Recipient Name</Label>
                        <Input
                          id="recipient"
                          placeholder="Who is this for?"
                          value={recipientName}
                          onChange={(e) => setRecipientName(e.target.value)}
                        />
                      </div>
                    )}
                  </div>

                  <Button 
                    onClick={sendNote}
                    className="w-full bg-gradient-romantic hover:opacity-90 text-white font-dancing text-lg py-3"
                  >
                    <Send className="w-5 h-5 mr-2" />
                    Send Love Note
                  </Button>
                </CardContent>
              </Card>
            </div>

            {/* Live Preview */}
            <div className="space-y-6">
              <Card className="confession-card">
                <CardHeader>
                  <CardTitle className="text-2xl font-dancing text-romantic">Preview</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className={`${selectedTemplate.bg} ${selectedTemplate.border} border-2 rounded-lg p-6 min-h-[400px] relative overflow-hidden`}>
                    {/* Floating hearts in preview */}
                    <div className="absolute inset-0 pointer-events-none">
                      {[...Array(5)].map((_, i) => (
                        <Heart
                          key={i}
                          className="absolute w-4 h-4 text-white/30 animate-pulse"
                          fill="currentColor"
                          style={{
                            left: `${Math.random() * 100}%`,
                            top: `${Math.random() * 100}%`,
                            animationDelay: `${i * 0.5}s`
                          }}
                        />
                      ))}
                    </div>

                    <div className="relative z-10 text-center text-white">
                      <h3 className="font-dancing text-3xl mb-4">💕 Love Note 💕</h3>
                      
                      {noteMessage ? (
                        <div className="bg-white/20 rounded-lg p-4 mb-4 backdrop-blur-sm">
                          <p className="text-lg leading-relaxed">{noteMessage}</p>
                        </div>
                      ) : (
                        <div className="bg-white/10 rounded-lg p-4 mb-4 backdrop-blur-sm">
                          <p className="text-white/70 italic">Your message will appear here...</p>
                        </div>
                      )}

                      {selectedEmojis.length > 0 && (
                        <div className="flex justify-center gap-2 mb-4">
                          {selectedEmojis.map((emoji, index) => (
                            <span key={index} className="text-2xl animate-bounce" style={{ animationDelay: `${index * 0.1}s` }}>
                              {emoji}
                            </span>
                          ))}
                        </div>
                      )}

                      <div className="text-sm">
                        From: {isAnonymous ? 'Secret Admirer 💕' : (user?.Name || 'Someone Special')}
                      </div>
                    </div>
                  </div>

                  <Button 
                    onClick={downloadNote}
                    variant="outline"
                    className="w-full mt-4 font-dancing"
                  >
                    <Download className="w-4 h-4 mr-2" />
                    Download Preview
                  </Button>
                </CardContent>
              </Card>
            </div>
          </div>
        ) : (
          /* Inbox */
          <div className="space-y-6">
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
              {mockReceivedNotes.map((note) => {
                const template = templates.find(t => t.id === note.template) || templates[0];
                return (
                  <Card 
                    key={note.id}
                    className="confession-card cursor-pointer hover:scale-105 transition-all duration-300"
                    onClick={() => setSelectedNote(note)}
                  >
                    <CardContent className="p-0">
                      <div className={`${template.bg} p-4 rounded-t-lg relative overflow-hidden`}>
                        <div className="text-center text-white relative z-10">
                          <MessageCircle className="w-8 h-8 mx-auto mb-2" fill="currentColor" />
                          <p className="text-sm font-dancing">From: {note.from}</p>
                        </div>
                        <div className="absolute inset-0 pointer-events-none">
                          {note.emojis.map((emoji, index) => (
                            <span
                              key={index}
                              className="absolute text-2xl opacity-30"
                              style={{
                                left: `${20 + index * 25}%`,
                                top: `${20 + index * 15}%`
                              }}
                            >
                              {emoji}
                            </span>
                          ))}
                        </div>
                      </div>
                      <div className="p-4">
                        <p className="text-sm text-muted-foreground line-clamp-2">{note.message}</p>
                        <p className="text-xs text-muted-foreground mt-2">
                          {note.timestamp.toLocaleDateString()} at {note.timestamp.toLocaleTimeString()}
                        </p>
                      </div>
                    </CardContent>
                  </Card>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {/* Full Page Note Modal */}
      {selectedNote && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="max-w-2xl w-full animate-scale-in">
            <div className="relative">
              <Button
                onClick={() => setSelectedNote(null)}
                variant="ghost"
                size="sm"
                className="absolute -top-12 right-0 text-white hover:bg-white/20"
              >
                <X className="w-5 h-5" />
              </Button>
              
              <Card className="confession-card">
                <CardContent className="p-0">
                  {(() => {
                    const template = templates.find(t => t.id === selectedNote.template) || templates[0];
                    return (
                      <div className={`${template.bg} p-8 rounded-lg relative overflow-hidden min-h-[500px] flex flex-col justify-center`}>
                        <div className="absolute inset-0 pointer-events-none">
                          {[...Array(10)].map((_, i) => (
                            <Heart
                              key={i}
                              className="absolute w-6 h-6 text-white/20 animate-pulse"
                              fill="currentColor"
                              style={{
                                left: `${Math.random() * 100}%`,
                                top: `${Math.random() * 100}%`,
                                animationDelay: `${i * 0.3}s`
                              }}
                            />
                          ))}
                        </div>

                        <div className="relative z-10 text-center text-white">
                          <h2 className="font-dancing text-5xl mb-8">💌 Love Note 💌</h2>
                          
                          <div className="bg-white/20 rounded-lg p-6 mb-6 backdrop-blur-sm">
                            <p className="text-xl leading-relaxed">{selectedNote.message}</p>
                          </div>

                          <div className="flex justify-center gap-3 mb-6">
                            {selectedNote.emojis.map((emoji, index) => (
                              <span 
                                key={index} 
                                className="text-4xl animate-bounce" 
                                style={{ animationDelay: `${index * 0.2}s` }}
                              >
                                {emoji}
                              </span>
                            ))}
                          </div>

                          <div className="space-y-2">
                            <p className="text-lg font-dancing">
                              With love from: {selectedNote.from} 💕
                            </p>
                            <p className="text-sm opacity-80">
                              {selectedNote.timestamp.toLocaleDateString()} at {selectedNote.timestamp.toLocaleTimeString()}
                            </p>
                          </div>
                        </div>
                      </div>
                    );
                  })()}
                </CardContent>
              </Card>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};