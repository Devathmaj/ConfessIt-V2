import { useState, useEffect, useRef } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Textarea } from '@/components/ui/textarea';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import {
    Mail,
    Palette,
    Send,
    Download,
    X,
    MessageCircle
} from 'lucide-react';
import { toast } from 'sonner';

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
    emojis: string[]; // Kept as array for mock data compatibility
}

const emojiStickers = ['💕', '💖', '💗', '💘', '💝', '💌', '🌹', '🌸', '🌺', '🦋', '✨', '�', '⭐', '🌟', '💎', '👑'];
const availableFonts = ['sans-serif', 'serif', 'monospace', 'cursive'];
const availableFontStyles = ['normal', 'bold', 'italic'];


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

// Mock components to replace unresolved imports
const PlaceholderComponent = ({ name }: { name: string }) => (
    <div className="hidden" aria-hidden="true">{name} component placeholder</div>
);

export const LoveNotesPage = () => {
    const [activeTab, setActiveTab] = useState<'create' | 'inbox'>('create');
    const [templates, setTemplates] = useState<Template[]>([]);
    const [selectedTemplate, setSelectedTemplate] = useState<Template | null>(null);
    const [noteMessage, setNoteMessage] = useState('');
    const [isAnonymous, setIsAnonymous] = useState(false);
    const [selectedEmoji, setSelectedEmoji] = useState<string | null>(null);
    const [selectedFont, setSelectedFont] = useState<string>('sans-serif');
    const [selectedFontStyle, setSelectedFontStyle] = useState<string>('normal');
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
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';

                let textToRender = '';
                let fontStyle = 'normal';
                let fontWeight = 'normal';
                const fontFamily = selectedFont;
                const fontSize = field.font?.size || 16;

                switch (field.name) {
                    case 'message_text':
                        textToRender = noteMessage || 'Your message here...';
                        if (selectedFontStyle === 'italic') fontStyle = 'italic';
                        if (selectedFontStyle === 'bold') fontWeight = 'bold';
                        break;
                    case 'from_label':
                        textToRender = field.value || 'From,';
                        fontWeight = '900'; // Enforce extra bold
                        break;
                    case 'sender_name':
                        textToRender = isAnonymous ? 'Anonymous' : 'Your Name';
                        fontWeight = '900'; // Enforce extra bold
                        break;
                    case 'emoji':
                        if (selectedEmoji) {
                            // Used to save the current context state
                            const originalFont = ctx.font;
                            // Used to set a much larger font size specifically for the emoji sticker
                            ctx.font = `120px sans-serif`;
                            ctx.fillText(selectedEmoji, field.x + field.width / 2, field.y + field.height / 2);
                            // Used to restore the original font for other fields
                            ctx.font = originalFont;
                        }
                        return; // Skip default text rendering for emoji
                }
                
                // Used to construct and set the font string for the current field
                ctx.font = `${fontStyle} ${fontWeight} ${fontSize}px ${fontFamily}`;
                
                // Used to wrap text and render it on the canvas
                if (field.type !== 'emoji') {
                    wrapText(ctx, textToRender, field.x, field.y, field.width, field.height, fontSize);
                }
            });
        };

    }, [selectedTemplate, noteMessage, isAnonymous, selectedEmoji, selectedFont, selectedFontStyle]);

    // Used to wrap and center text within a specified area.
    const wrapText = (context: CanvasRenderingContext2D, text: string, x: number, y: number, maxWidth: number, maxHeight: number, lineHeight: number) => {
        const lines: string[] = [];
        // Used to split text by newline characters to respect manual line breaks.
        const paragraphs = text.split('\n');

        for (const paragraph of paragraphs) {
            // Used to handle word wrapping for each paragraph.
            const words = paragraph.split(' ');
            let currentLine = '';
            for (const word of words) {
                const testLine = currentLine ? `${currentLine} ${word}` : word;
                const metrics = context.measureText(testLine);
                if (metrics.width > maxWidth && currentLine) {
                    lines.push(currentLine);
                    currentLine = word;
                } else {
                    currentLine = testLine;
                }
            }
            lines.push(currentLine);
        }
        
        // Used to handle lines that are still too wide (e.g., a single long word).
        const wrappedLines: string[] = [];
        lines.forEach(line => {
            let tempLine = '';
            if (context.measureText(line).width > maxWidth) {
                // Used to break the line character by character if it's too wide.
                for(let i = 0; i < line.length; i++) {
                    const char = line[i];
                    const testLine = tempLine + char;
                    if (context.measureText(testLine).width > maxWidth) {
                        wrappedLines.push(tempLine);
                        tempLine = char;
                    } else {
                        tempLine = testLine;
                    }
                }
                wrappedLines.push(tempLine);
            } else {
                // Used to add the line as is if it fits.
                wrappedLines.push(line);
            }
        });

        // Used to determine which lines are visible based on the maxHeight.
        const maxLines = Math.floor(maxHeight / lineHeight);
        const visibleLines = wrappedLines.slice(0, maxLines);

        // Used to add an ellipsis if the text is truncated.
        if (wrappedLines.length > maxLines && visibleLines.length > 0) {
            const lastLineIndex = visibleLines.length - 1;
            let lastLine = visibleLines[lastLineIndex];
            let truncatedLine = lastLine;
            while (context.measureText(truncatedLine + '...').width > maxWidth && truncatedLine.length > 0) {
                truncatedLine = truncatedLine.slice(0, -1);
            }
            visibleLines[lastLineIndex] = truncatedLine + '...';
        }

        // Used to calculate the total height of the rendered text block.
        const totalTextHeight = visibleLines.length * lineHeight;
        // Used to calculate the starting Y position to vertically center the text block.
        const startY = (y + maxHeight / 2) - (totalTextHeight / 2) + (lineHeight / 2);

        // Used to draw the visible lines onto the canvas.
        visibleLines.forEach((line, index) => {
            const lineY = startY + (index * lineHeight);
            context.fillText(line, x + maxWidth / 2, lineY);
        });
    };

    // Used to handle the selection of a single emoji sticker.
    const handleEmojiSelect = (emoji: string) => {
        setSelectedEmoji(prev => (prev === emoji ? null : emoji)); // Toggles selection
    };

    // Used to send the note
    const sendNote = () => {
        if (!noteMessage.trim()) {
            toast.error('Please write a message!');
            return;
        }

        toast.success('Love note sent! 💕');
        setNoteMessage('');
        setSelectedEmoji(null);
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
            <PlaceholderComponent name="Navigation" />
            <PlaceholderComponent name="FloatingHearts" />

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
                    <PlaceholderComponent name="CountdownTimer" />
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
                     <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
                     {/* Left Column: Editor */}
                     <div className="lg:col-span-1 space-y-6">
                         <Card className="confession-card">
                             <CardHeader>
                                 <CardTitle className="text-2xl font-dancing text-romantic">Customize Your Note</CardTitle>
                             </CardHeader>
                             <CardContent className="space-y-6">
                                 {/* Template Selection */}
                                 <div>
                                     <Label className="text-romantic font-semibold mb-3 block">Choose a Template</Label>
                                     <div className="grid grid-cols-3 gap-3">
                                         {templates.map((template) => (
                                             <button
                                                 key={template.template_name}
                                                 onClick={() => setSelectedTemplate(template)}
                                                 className={`p-2 rounded-lg border-2 transition-all duration-300 hover:scale-105 ${selectedTemplate?.template_name === template.template_name
                                                         ? 'border-romantic'
                                                         : 'border-muted hover:border-romantic'
                                                     }`}
                                             >
                                                 <img src={`/templates/${template.image}`} alt={template.template_name} className="w-full h-auto rounded" />
                                             </button>
                                         ))}
                                     </div>
                                 </div>

                                 {/* Font Selection */}
                                 <div>
                                     <Label className="text-romantic font-semibold mb-3 block">Choose a Font</Label>
                                     <div className="flex flex-wrap gap-2">
                                         {availableFonts.map((font) => (
                                             <button
                                                 key={font}
                                                 onClick={() => setSelectedFont(font)}
                                                 className={`px-4 py-2 text-sm rounded-lg border-2 transition-all duration-300 hover:scale-105 ${selectedFont === font
                                                         ? 'border-romantic bg-gradient-love text-white'
                                                         : 'border-muted hover:border-romantic'
                                                     }`}
                                                 style={{ fontFamily: font }}
                                             >
                                                 {font}
                                             </button>
                                         ))}
                                     </div>
                                 </div>

                                 {/* Font Style Selection */}
                                 <div>
                                     <Label className="text-romantic font-semibold mb-3 block">Choose a Style</Label>
                                     <div className="flex flex-wrap gap-2">
                                         {availableFontStyles.map((style) => (
                                             <button
                                                 key={style}
                                                 onClick={() => setSelectedFontStyle(style)}
                                                 className={`px-4 py-2 text-sm rounded-lg border-2 transition-all duration-300 hover:scale-105 capitalize ${selectedFontStyle === style
                                                         ? 'border-romantic bg-gradient-love text-white'
                                                         : 'border-muted hover:border-romantic'
                                                     }`}
                                                 style={{ 
                                                     fontWeight: style === 'bold' ? 'bold' : 'normal',
                                                     fontStyle: style === 'italic' ? 'italic' : 'normal'
                                                 }}
                                             >
                                                 {style}
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
                                     <Label className="text-romantic font-semibold mb-3 block">Add a Sticker</Label>
                                     <div className="grid grid-cols-8 gap-2">
                                         {emojiStickers.map((emoji) => (
                                             <button
                                                 key={emoji}
                                                 onClick={() => handleEmojiSelect(emoji)}
                                                 className={`w-10 h-10 text-lg rounded-lg border-2 transition-all duration-300 hover:scale-110 ${selectedEmoji === emoji
                                                         ? 'border-romantic bg-gradient-love'
                                                         : 'border-muted hover:border-romantic'
                                                     }`}
                                             >
                                                 {emoji}
                                             </button>
                                         ))}
                                     </div>
                                 </div>
                             </CardContent>
                         </Card>
                     </div>

                     {/* Center Column: Live Preview */}
                     <div className="lg:col-span-2 space-y-6">
                         <Card className="confession-card sticky top-24">
                             <CardHeader>
                                 <CardTitle className="text-2xl font-dancing text-romantic">Live Preview</CardTitle>
                             </CardHeader>
                             <CardContent>
                                 <canvas ref={canvasRef} className="w-full h-auto rounded-lg border-2 border-muted shadow-lg" />
                                 <div className="flex items-center justify-between mt-4">
                                     <div className="flex items-center space-x-2">
                                         <Switch id="anonymous-switch" checked={isAnonymous} onCheckedChange={setIsAnonymous} />
                                         <Label htmlFor="anonymous-switch" className="text-romantic font-semibold">Send Anonymously</Label>
                                     </div>
                                     <Button
                                         onClick={downloadNote}
                                         variant="outline"
                                         className="font-dancing"
                                     >
                                         <Download className="w-4 h-4 mr-2" />
                                         Download
                                     </Button>
                                 </div>
                                 <Button
                                     onClick={sendNote}
                                     className="w-full bg-gradient-romantic hover:opacity-90 text-white font-dancing text-lg py-3 mt-4"
                                 >
                                     <Send className="w-5 h-5 mr-2" />
                                     Send Your Love Note
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
