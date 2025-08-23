import { useState, useEffect, useRef } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Textarea } from '@/components/ui/textarea';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import { CountdownTimer } from '@/components/ui/countdown-timer';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import {
    Mail,
    Palette,
    Send,
    Download,
    X,
    MessageCircle
} from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';

// Used to define the structure for a template's field
interface TemplateField {
    name: string;
    type: 'text' | 'static_text' | 'emoji';
    value?: string;
    x: number;
    y: number;
    width: number;
    height: number;
    font?: {
        size: number;
        color: string;
        style: string;
    };
}

// Used to define the structure for a template
interface Template {
    template_name: string;
    image: string;
    fields: TemplateField[];
}

// Used to define the structure for a received love note
interface LoveNote {
    id: string;
    from: string;
    message: string;
    template: string;
    anonymous: boolean;
    timestamp: Date;
    emojis: string[];
}

const emojiStickers = ['💕', '💖', '💗', '💘', '💝', '💌', '🌹', '🌸', '🌺', '🦋', '✨', '💫', '⭐', '🌟', '💎', '👑'];

// Mock data for received notes
const mockReceivedNotes: LoveNote[] = [
    {
        id: '1',
        from: 'Secret Admirer',
        message: 'Your smile brightens my day more than sunshine ☀️',
        template: 'template_1.png',
        anonymous: true,
        timestamp: new Date('2024-02-14T10:30:00'),
        emojis: ['💕', '🌟', '✨']
    },
    {
        id: '2',
        from: 'Alex Chen',
        message: 'Thanks for being such an amazing friend! Hope your day is filled with love 💖',
        template: 'template_2.png',
        anonymous: false,
        timestamp: new Date('2024-02-14T14:15:00'),
        emojis: ['🌸', '💖', '🦋']
    },
];

export const LoveNotesPage = () => {
    const { user } = useAuth();
    const [activeTab, setActiveTab] = useState<'create' | 'inbox'>('create');
    const [templates, setTemplates] = useState<Template[]>([]);
    const [selectedTemplate, setSelectedTemplate] = useState<Template | null>(null);
    const [noteMessage, setNoteMessage] = useState('');
    const [isAnonymous, setIsAnonymous] = useState(false);
    const [selectedEmojis, setSelectedEmojis] = useState<string[]>([]);
    const [selectedNote, setSelectedNote] = useState<LoveNote | null>(null);
    const canvasRef = useRef<HTMLCanvasElement>(null);

    // Used to fetch templates from the public directory
    useEffect(() => {
        const fetchTemplates = async () => {
            try {
                const templatePromises = Array.from({ length: 6 }, (_, i) =>
                    fetch(`/templates/template_${i + 1}.json`).then(res => res.json())
                );
                const loadedTemplates = await Promise.all(templatePromises);
                setTemplates(loadedTemplates);
                setSelectedTemplate(loadedTemplates[0]);
            } catch (error) {
                console.error("Failed to load templates:", error);
                toast.error("Could not load the note templates. Please try again later.");
            }
        };

        fetchTemplates();
    }, []);

    // Used to draw the content on the canvas
    useEffect(() => {
        if (!selectedTemplate || !canvasRef.current) return;

        const canvas = canvasRef.current;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;

        const img = new Image();
        img.src = `/templates/${selectedTemplate.image}`;
        img.onload = () => {
            canvas.width = img.width;
            canvas.height = img.height;
            ctx.drawImage(img, 0, 0);

            // Used to render dynamic fields on the template
            selectedTemplate.fields.forEach(field => {
                ctx.fillStyle = field.font?.color || '#000000';
                ctx.font = `${field.font?.style || 'normal'} ${field.font?.size || 16}px sans-serif`;
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';

                let textToRender = '';

                switch (field.name) {
                    case 'message_text':
                        textToRender = noteMessage || 'Your message here...';
                        break;
                    case 'from_label':
                        textToRender = field.value || 'From';
                        break;
                    case 'sender_name':
                        textToRender = isAnonymous ? 'Anonymous' : (user?.Name || 'You');
                        break;
                    case 'emoji':
                        if (selectedEmojis.length > 0) {
                            const emojiString = selectedEmojis.join(' ');
                            ctx.fillText(emojiString, field.x + field.width / 2, field.y + field.height / 2);
                        }
                        return; // Skip default text rendering for emoji
                }

                // Wrap text and render
                wrapText(ctx, textToRender, field.x, field.y, field.width, field.font?.size || 16);
            });
        };

    }, [selectedTemplate, noteMessage, isAnonymous, selectedEmojis, user]);

    // Used to wrap text within a specified area
    const wrapText = (context: CanvasRenderingContext2D, text: string, x: number, y: number, maxWidth: number, lineHeight: number) => {
        const words = text.split(' ');
        let line = '';
        let currentY = y;

        for (let n = 0; n < words.length; n++) {
            const testLine = line + words[n] + ' ';
            const metrics = context.measureText(testLine);
            const testWidth = metrics.width;
            if (testWidth > maxWidth && n > 0) {
                context.fillText(line, x + maxWidth / 2, currentY);
                line = words[n] + ' ';
                currentY += lineHeight;
            } else {
                line = testLine;
            }
        }
        context.fillText(line, x + maxWidth / 2, currentY);
    };

    // Used to toggle an emoji in the selection
    const toggleEmoji = (emoji: string) => {
        setSelectedEmojis(prev =>
            prev.includes(emoji)
                ? prev.filter(e => e !== emoji)
                : [...prev, emoji]
        );
    };

    // Used to send the note
    const sendNote = () => {
        if (!noteMessage.trim()) {
            toast.error('Please write a message!');
            return;
        }

        toast.success('Love note sent! 💕');
        setNoteMessage('');
        setSelectedEmojis([]);
    };

    // Used to download the note as an image
    const downloadNote = () => {
        const canvas = canvasRef.current;
        if (canvas) {
            const image = canvas.toDataURL('image/png');
            const link = document.createElement('a');
            link.href = image;
            link.download = `${selectedTemplate?.template_name || 'love-note'}.png`;
            link.click();
            toast.success('Note downloaded! 📱');
        }
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
                                        <div className="grid grid-cols-3 gap-3">
                                            {templates.map((template) => (
                                                <button
                                                    key={template.template_name}
                                                    onClick={() => setSelectedTemplate(template)}
                                                    className={`p-2 rounded-lg border-2 transition-all duration-300 hover:scale-105 ${selectedTemplate?.template_name === template.template_name
                                                            ? `border-romantic`
                                                            : 'border-muted hover:border-romantic'
                                                        }`}
                                                >
                                                    <img src={`/templates/${template.image}`} alt={template.template_name} className="w-full h-auto rounded" />
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
                                                    className={`w-10 h-10 text-lg rounded-lg border-2 transition-all duration-300 hover:scale-110 ${selectedEmojis.includes(emoji)
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
                                    <canvas ref={canvasRef} className="w-full h-auto rounded-lg border-2 border-muted" />
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
                                const template = templates.find(t => t.image === note.template);
                                return (
                                    <Card
                                        key={note.id}
                                        className="confession-card cursor-pointer hover:scale-105 transition-all duration-300"
                                        onClick={() => setSelectedNote(note)}
                                    >
                                        <CardContent className="p-0">
                                            <div className={`p-4 rounded-t-lg relative overflow-hidden`}>
                                                {template && <img src={`/templates/${template.image}`} alt="note background" className="absolute inset-0 w-full h-full object-cover" />}
                                                <div className="text-center text-white relative z-10 bg-black/30 p-2 rounded">
                                                    <MessageCircle className="w-8 h-8 mx-auto mb-2" fill="currentColor" />
                                                    <p className="text-sm font-dancing">From: {note.from}</p>
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
                                        const template = templates.find(t => t.image === selectedNote.template);
                                        return (
                                            <div className={`p-8 rounded-lg relative overflow-hidden min-h-[500px] flex flex-col justify-center`}>
                                                {template && <img src={`/templates/${template.image}`} alt="note background" className="absolute inset-0 w-full h-full object-cover -z-10" />}
                                                <div className="absolute inset-0 bg-black/40 -z-10"></div>


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
