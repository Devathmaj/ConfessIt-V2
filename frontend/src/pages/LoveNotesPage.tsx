// src/pages/LoveNotesPage.tsx

import { useState, useEffect, useRef, useMemo } from 'react';
import { useAuth, User } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Textarea } from '@/components/ui/textarea';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import { CountdownTimer } from '@/components/ui/countdown-timer';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
    Mail,
    Palette,
    Send,
    Download,
    X,
    MessageCircle,
    Users,
    Search,
    Info
} from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';
import { getAllUsers, getClasses, sendLoveNote } from '@/services/api';

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
    emojis: string[];
}

// Used to define the structure for a user in the recipient list
interface Recipient extends User {
    _id: string;
}

const emojiStickers = ['💕', '💖', '💗', '💘', '💝', '💌', '🌹', '🌸', '🌺', '🦋', '✨', '💫', '⭐', '🌟', '💎', '👑'];
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
        emojis: ['☀️', '🌟', '✨']
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
    const { user, fetchAndSetUser, token } = useAuth();
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

    // State for recipient selection modal
    const [isRecipientModalOpen, setIsRecipientModalOpen] = useState(false);
    const [allUsers, setAllUsers] = useState<Recipient[]>([]);
    const [classes, setClasses] = useState<string[]>([]);
    const [searchTerm, setSearchTerm] = useState('');
    const [selectedClass, setSelectedClass] = useState('All');
    const [selectedRecipient, setSelectedRecipient] = useState<Recipient | null>(null);


    // Used to fetch templates, users, and classes
    useEffect(() => {
        const fetchInitialData = async () => {
            try {
                // Fetch templates
                const templatePromises = Array.from({ length: 6 }, (_, i) =>
                    fetch(`/templates/template_${i + 1}.json`).then(res => res.json())
                );
                const loadedTemplates = await Promise.all(templatePromises);
                setTemplates(loadedTemplates);
                setSelectedTemplate(loadedTemplates[0]);

                // Fetch users and classes
                const [usersData, classesData] = await Promise.all([
                    getAllUsers(),
                    getClasses()
                ]);
                setAllUsers(usersData);
                setClasses(['All', ...classesData]);

            } catch (error) {
                console.error("Failed to load initial data:", error);
                toast.error("Could not load necessary data. Please try again later.");
            }
        };

        fetchInitialData();
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
                        textToRender = isAnonymous ? 'Anonymous' : (user?.Name || 'Your Name');
                        fontWeight = '900'; // Enforce extra bold
                        break;
                    case 'emoji':
                        if (selectedEmoji) {
                            const originalFont = ctx.font;
                            ctx.font = `120px sans-serif`;
                            ctx.fillText(selectedEmoji, field.x + field.width / 2, field.y + field.height / 2);
                            ctx.font = originalFont;
                        }
                        return; 
                }
                
                ctx.font = `${fontStyle} ${fontWeight} ${fontSize}px ${fontFamily}`;
                
                if (field.type !== 'emoji') {
                    wrapText(ctx, textToRender, field.x, field.y, field.width, field.height, fontSize);
                }
            });
        };

    }, [selectedTemplate, noteMessage, isAnonymous, selectedEmoji, selectedFont, selectedFontStyle, user]);

    // Used to wrap and center text within a specified area.
    const wrapText = (context: CanvasRenderingContext2D, text: string, x: number, y: number, maxWidth: number, maxHeight: number, lineHeight: number) => {
        const lines: string[] = [];
        const paragraphs = text.split('\n');

        for (const paragraph of paragraphs) {
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
        
        const wrappedLines: string[] = [];
        lines.forEach(line => {
            let tempLine = '';
            if (context.measureText(line).width > maxWidth) {
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
                wrappedLines.push(line);
            }
        });

        const maxLines = Math.floor(maxHeight / lineHeight);
        const visibleLines = wrappedLines.slice(0, maxLines);

        if (wrappedLines.length > maxLines && visibleLines.length > 0) {
            const lastLineIndex = visibleLines.length - 1;
            let lastLine = visibleLines[lastLineIndex];
            let truncatedLine = lastLine;
            while (context.measureText(truncatedLine + '...').width > maxWidth && truncatedLine.length > 0) {
                truncatedLine = truncatedLine.slice(0, -1);
            }
            visibleLines[lastLineIndex] = truncatedLine + '...';
        }

        const totalTextHeight = visibleLines.length * lineHeight;
        const startY = (y + maxHeight / 2) - (totalTextHeight / 2) + (lineHeight / 2);

        visibleLines.forEach((line, index) => {
            const lineY = startY + (index * lineHeight);
            context.fillText(line, x + maxWidth / 2, lineY);
        });
    };

    // Used to handle the selection of a single emoji sticker.
    const handleEmojiSelect = (emoji: string) => {
        setSelectedEmoji(prev => (prev === emoji ? null : emoji));
    };
    
    // Used to open the recipient modal after validation
    const handleOpenRecipientModal = () => {
        if (user?.isLovenotesSend) {
            toast.info("You've already sent your love note this season!");
            return;
        }
        if (!noteMessage.trim()) {
            toast.error('Please write a message before sending!');
            return;
        }
        setIsRecipientModalOpen(true);
    };

    // Used to send the note after confirming recipient
    const handleConfirmAndSend = async () => {
        if (!selectedRecipient || !canvasRef.current) {
            toast.error("Please select a recipient.");
            return;
        }

        const imageBase64 = canvasRef.current.toDataURL('image/png');

        try {
            await sendLoveNote({
                recipient_id: selectedRecipient._id,
                image_base64: imageBase64,
                message_text: noteMessage,
                is_anonymous: isAnonymous,
            });
            toast.success('Love note sent for review! 💕');
            setIsRecipientModalOpen(false);
            setNoteMessage('');
            setSelectedEmoji(null);
            setSelectedRecipient(null);
            setSearchTerm('');
            setSelectedClass('All');
            // Used to refresh user data to get the updated isLovenotesSend status
            if (token) {
                fetchAndSetUser(token);
            }
        } catch (error: any) {
            console.error("Failed to send love note:", error);
            const errorMessage = error.response?.data?.detail || "Failed to send love note. Please try again.";
            toast.error(errorMessage);
        }
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

    // Used to filter users based on search term and class
    const filteredUsers = useMemo(() => {
        return allUsers.filter(u => {
            const nameMatch = u.Name.toLowerCase().includes(searchTerm.toLowerCase());
            const classMatch = selectedClass === 'All' || u.which_class === selectedClass;
            return nameMatch && classMatch;
        });
    }, [allUsers, searchTerm, selectedClass]);

    return (
        <>
            <div className="min-h-screen p-4 pt-24">
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
                                        onClick={handleOpenRecipientModal}
                                        disabled={user?.isLovenotesSend}
                                        className="w-full bg-gradient-romantic hover:opacity-90 text-white font-dancing text-lg py-3 mt-4"
                                    >
                                        <Send className="w-5 h-5 mr-2" />
                                        {user?.isLovenotesSend ? "You've Already Sent Your Note" : "Send Your Love Note"}
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
            </div>

            {/* Recipient Selection Modal */}
            <Dialog open={isRecipientModalOpen} onOpenChange={setIsRecipientModalOpen}>
                <DialogContent className="sm:max-w-[425px] md:max-w-lg lg:max-w-xl confession-card">
                    <DialogHeader>
                        <DialogTitle className="font-dancing text-3xl text-romantic">Select Your Recipient</DialogTitle>
                        <DialogDescription>
                            Find the special someone to send your love note to.
                        </DialogDescription>
                    </DialogHeader>
                    <div className="space-y-4 py-4">
                        {/* Important Information */}
                        <div className="bg-yellow-100 border-l-4 border-yellow-500 text-yellow-700 p-4 rounded-md" role="alert">
                            <p className="font-bold flex items-center"><Info className="w-5 h-5 mr-2" />Important</p>
                            <ul className="list-disc list-inside mt-2 text-sm space-y-1">
                                <li>You can only send <strong>one</strong> Love Note this season. Are you sure you want to use it now?</li>
                                <li>All notes are <strong>reviewed by moderators</strong> before being delivered to the recipient.</li>
                            </ul>
                        </div>

                        {/* Filters */}
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                            <div className="relative">
                                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                                <Input
                                    placeholder="Search by name..."
                                    value={searchTerm}
                                    onChange={(e) => setSearchTerm(e.target.value)}
                                    className="pl-10"
                                />
                            </div>
                            <Select value={selectedClass} onValueChange={setSelectedClass}>
                                <SelectTrigger>
                                    <SelectValue placeholder="Filter by class" />
                                </SelectTrigger>
                                <SelectContent>
                                    {classes.map(c => (
                                        <SelectItem key={c} value={c}>{c}</SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>

                        {/* User List */}
                        <ScrollArea className="h-64 border rounded-md p-2">
                            <div className="space-y-2">
                                {filteredUsers.length > 0 ? filteredUsers.map(u => (
                                    <div
                                        key={u._id}
                                        onClick={() => setSelectedRecipient(u)}
                                        className={`flex items-center p-3 rounded-md cursor-pointer transition-all duration-200 ${selectedRecipient?._id === u._id ? 'bg-romantic/20 border-romantic border' : 'hover:bg-muted/50'}`}
                                    >
                                        <img
                                            src={`http://localhost:8001/profile_pictures/${u.profile_picture_id || 'default.png'}`}
                                            alt={u.Name}
                                            className="w-10 h-10 rounded-full mr-4 object-cover"
                                            onError={(e) => { e.currentTarget.src = 'https://via.placeholder.com/40' }}
                                        />
                                        <div>
                                            <p className="font-semibold">{u.Name}</p>
                                            <p className="text-sm text-muted-foreground">{u.which_class}</p>
                                        </div>
                                    </div>
                                )) : (
                                    <p className="text-center text-muted-foreground py-8">No users found.</p>
                                )}
                            </div>
                        </ScrollArea>
                        {selectedRecipient && (
                            <p className="text-center text-sm text-romantic font-semibold">
                                Selected: {selectedRecipient.Name}
                            </p>
                        )}
                    </div>
                    <DialogFooter>
                        <Button
                            onClick={handleConfirmAndSend}
                            disabled={!selectedRecipient}
                            className="w-full bg-gradient-romantic hover:opacity-90 text-white font-dancing text-lg py-3"
                        >
                            <Send className="w-5 h-5 mr-2" />
                            Confirm & Send Note
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

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

                            <Card>
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
        </>
    );
};
