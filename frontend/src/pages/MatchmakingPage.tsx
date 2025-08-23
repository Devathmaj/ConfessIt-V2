import { useState } from 'react';
import { useAuth, User } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { CountdownTimer } from '@/components/ui/countdown-timer';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { findMatch } from '@/services/api';
import { 
  Heart, 
  MessageCircle, 
  Sparkles, 
  ArrowLeft,
  Send
} from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';

const prewrittenMessages = [
  "Hey! I love your profile, would you like to chat? 💕",
  "Your interests caught my eye! What's your favorite book? 📚",
  "Coffee lover here too! Know any good spots? ☕",
  "Your bio made me smile! Tell me more about yourself ✨"
];

const icebreakerCards = [
  "If you could have dinner with anyone, who would it be?",
  "What's your idea of a perfect day?",
  "Share your most embarrassing moment!",
  "What's on your bucket list?",
  "Describe yourself in three emojis!"
];

// --- MOCK DATA FOR ANIMATION ---
// Used to provide a pool of fake user profiles for the slot machine animation.
// The profile_picture_id now uses a placeholder service to avoid 404 errors.
const mockMatches: User[] = [
    { Name: 'Alex', Regno: '123', emoji: '😊', username: 'alex', which_class: 'CSE', profile_picture_id: 'https://placehold.co/96x96/fecdd3/be123c?text=A', interests: ['Gaming', 'Music'], bio: 'Just a mock user.' },
    { Name: 'Jordan', Regno: '456', emoji: '😎', username: 'jordan', which_class: 'ECE', profile_picture_id: 'https://placehold.co/96x96/fecdd3/be123c?text=J', interests: ['Sports', 'Movies'], bio: 'Another mock user.' },
    { Name: 'Taylor', Regno: '789', emoji: '🤩', username: 'taylor', which_class: 'MECH', profile_picture_id: 'https://placehold.co/96x96/fecdd3/be123c?text=T', interests: ['Reading', 'Hiking'], bio: 'A third mock user.' },
    { Name: 'Casey', Regno: '101', emoji: '🥳', username: 'casey', which_class: 'CIVIL', profile_picture_id: 'https://placehold.co/96x96/fecdd3/be123c?text=C', interests: ['Art', 'Cooking'], bio: 'You get the idea.' },
    { Name: 'Morgan', Regno: '112', emoji: '😇', username: 'morgan', which_class: 'CSE', profile_picture_id: 'https://placehold.co/96x96/fecdd3/be123c?text=M', interests: ['Photography', 'Travel'], bio: 'Almost done.' },
    { Name: 'Riley', Regno: '131', emoji: '🤓', username: 'riley', which_class: 'ECE', profile_picture_id: 'https://placehold.co/96x96/fecdd3/be123c?text=R', interests: ['Coding', 'Sci-Fi'], bio: 'Last one!' },
];

/**
 * Renders a 3D glass-like heart that visually fills with a red liquid
 * based on the provided fill percentage. This component uses SVG filters,
 * gradients, and clipping paths to achieve its effect.
 */
const GlassHeart = ({ fillPercentage }: { fillPercentage: number }) => {
    const heartPathData = "M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z";
    const viewBoxHeight = 24;
    const liquidY = viewBoxHeight * (1 - fillPercentage / 100);

    return (
        <div className="relative w-full h-full group-hover:scale-105 transition-transform duration-300">
            <svg
                viewBox="0 0 24 24"
                className="w-full h-full"
                style={{ filter: 'drop-shadow(0 5px 10px rgba(220, 38, 38, 0.4))' }}
            >
                <defs>
                    <clipPath id="heart-clip">
                        <path d={heartPathData} />
                    </clipPath>
                    <linearGradient id="liquid-gradient" x1="0%" y1="0%" x2="0%" y2="100%">
                        <stop offset="0%" stopColor="#f43f5e" />
                        <stop offset="100%" stopColor="#be123c" />
                    </linearGradient>
                </defs>
                <g clipPath="url(#heart-clip)">
                    <rect
                        x="0"
                        y={liquidY}
                        width="24"
                        height="24"
                        fill="url(#liquid-gradient)"
                        style={{ transition: 'y 0.1s linear' }}
                    />
                    <path fill="url(#liquid-gradient)">
                        <animate
                            attributeName="d"
                            dur="2s"
                            repeatCount="indefinite"
                            values={
                                `M 0 ${liquidY} C 6 ${liquidY - 1}, 6 ${liquidY - 1}, 12 ${liquidY} S 18 ${liquidY + 1}, 24 ${liquidY} V 24 H 0 Z;` +
                                `M 0 ${liquidY} C 6 ${liquidY + 1}, 6 ${liquidY + 1}, 12 ${liquidY} S 18 ${liquidY - 1}, 24 ${liquidY} V 24 H 0 Z;` +
                                `M 0 ${liquidY} C 6 ${liquidY - 1}, 6 ${liquidY - 1}, 12 ${liquidY} S 18 ${liquidY + 1}, 24 ${liquidY} V 24 H 0 Z`
                            }
                            style={{ transition: 'd 0.1s linear' }}
                        />
                    </path>
                </g>
                <path
                    d={heartPathData}
                    strokeWidth="0.5"
                    stroke="rgba(255, 255, 255, 0.7)"
                    fill="rgba(255, 255, 255, 0.2)"
                />
                <path
                    d="M 10 5 A 6 6 0 0 1 16 8 L 12 12 L 8 9 A 6 6 0 0 1 10 5 Z"
                    fill="rgba(255, 255, 255, 0.7)"
                    transform="rotate(-20 12 12)"
                    style={{ filter: 'blur(2px)' }}
                />
            </svg>
        </div>
    );
};


export const MatchmakingPage = () => {
  const { user } = useAuth();
  const [isMatching, setIsMatching] = useState(false);
  const [matchedProfile, setMatchedProfile] = useState<User | null>(null);
  const [slotMachineUser, setSlotMachineUser] = useState<User | null>(null);
  const [heartFill, setHeartFill] = useState(0);
  const [showChat, setShowChat] = useState(false);
  const [chatMessages, setChatMessages] = useState<string[]>([]);

  // Used to determine the correct image source for a user profile.
  // It checks if the picture ID is a full URL (for mock data) or a backend ID.
  const getProfilePictureUrl = (pictureId?: string) => {
    if (!pictureId) {
      return 'https://placehold.co/96x96/fecdd3/be123c?text=💕';
    }
    // If the pictureId is a full URL (from our mock data), use it directly.
    if (pictureId.startsWith('http')) {
      return pictureId;
    }
    // Otherwise, construct the URL to the backend service.
    return `http://localhost:8001/profile_pictures/${pictureId}`;
  };

  const startMatching = async () => {
    setIsMatching(true);
    setMatchedProfile(null);
    setHeartFill(0);
    setShowChat(false);
    setChatMessages([]);

    const slotInterval = setInterval(() => {
      const randomIndex = Math.floor(Math.random() * mockMatches.length);
      setSlotMachineUser(mockMatches[randomIndex]);
    }, 30);

    const totalDuration = 10000;
    const intervalDuration = 100;
    const totalSteps = totalDuration / intervalDuration;
    let currentStep = 0;

    const fillInterval = setInterval(() => {
      currentStep++;
      const newFill = (currentStep / totalSteps) * 100;
      setHeartFill(newFill);

      if (currentStep >= totalSteps) {
          clearInterval(fillInterval);
          clearInterval(slotInterval);

          findMatch()
            .then(finalMatch => {
              setMatchedProfile(finalMatch);
              toast.success('Match found! 💕');
            })
            .catch(error => {
              toast.error(error.response?.data?.detail || 'Could not find a match.');
            })
            .finally(() => {
              setSlotMachineUser(null);
              setIsMatching(false);
            });
      }
    }, intervalDuration);
  };

  const sendMessage = (message: string) => {
    setChatMessages(prev => [...prev, `You: ${message}`]);
    
    setTimeout(() => {
      const responses = [
        "That's so sweet! 💕",
        "I'd love to chat more!",
        "You seem really interesting!",
        "Thanks for reaching out! ✨"
      ];
      const response = responses[Math.floor(Math.random() * responses.length)];
      setChatMessages(prev => [...prev, `${matchedProfile?.Name}: ${response}`]);
    }, 1000);
  };

  const sendIcebreaker = (card: string) => {
    setChatMessages(prev => [...prev, `You: ${card}`]);
    toast.success('Icebreaker sent! 🧊💕');
  };

  const UserCard = ({ profile }: { profile: User }) => (
    <Card className="confession-card">
      <CardHeader className="items-center text-center">
        <div className="w-24 h-24 rounded-full p-1 bg-secondary">
            <img 
                src={getProfilePictureUrl(profile.profile_picture_id)} 
                alt={profile.Name}
                className="w-full h-full rounded-full object-cover"
            />
        </div>
        <CardTitle className="text-2xl font-dancing text-romantic pt-2">{profile.Name}</CardTitle>
        <CardDescription>{profile.which_class}</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-3">
          <div className="text-sm">
            <span className="font-semibold text-romantic">Interests:</span>
            <div className="flex flex-wrap gap-2 mt-1">
              {profile.interests?.map((interest) => (
                <span key={interest} className="px-2 py-1 bg-gradient-love rounded-full text-xs text-romantic-dark">
                  {interest}
                </span>
              ))}
            </div>
          </div>
          <p className="text-sm text-muted-foreground">
            "{profile.bio || 'No bio yet!'}"
          </p>
        </div>
      </CardContent>
    </Card>
  );

  return (
    <div className="min-h-screen p-4 pt-24 relative overflow-hidden">
      <Navigation />
      <FloatingHearts />
      
      <div className="max-w-7xl mx-auto mb-8">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-6">
          <div className="text-center lg:text-left">
            <h1 className="text-5xl font-dancing text-romantic mb-2">
              Random Matchmaking 💕
            </h1>
            <p className="text-xl text-muted-foreground">
              Find your perfect Valentine match!
            </p>
          </div>
          <CountdownTimer />
        </div>
      </div>

      <div className="max-w-7xl mx-auto">
        {!showChat ? (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-8 items-center">
            <div className="min-h-[400px] flex items-center justify-center bg-card rounded-lg p-4 border-2 border-dashed border-primary/20">
                <div className="w-full max-w-sm">
                    {user && <UserCard profile={user} />}
                </div>
            </div>

            <div className="flex flex-col items-center justify-center space-y-6 h-full">
              <div
                className="relative w-48 h-48 cursor-pointer group"
                onClick={isMatching ? undefined : startMatching}
              >
                <GlassHeart fillPercentage={heartFill} />
              </div>

              {!isMatching && !matchedProfile && (
                <p className="text-muted-foreground text-center font-dancing text-lg">
                  Click the heart to find a match!
                </p>
              )}

              {isMatching && (
                <p className="text-muted-foreground text-center font-dancing text-lg animate-pulse">
                  Finding your match...
                </p>
              )}

              {matchedProfile && (
                <Button
                  onClick={() => setShowChat(true)}
                  className="bg-gradient-romantic hover:opacity-90 text-white px-8 py-3 rounded-full font-dancing text-lg"
                >
                  <MessageCircle className="w-5 h-5 mr-2" />
                  Start Conversation
                </Button>
              )}
            </div>

            <div className="flex flex-col items-center justify-center gap-4">
                <div className="min-h-[400px] w-full flex items-center justify-center bg-card rounded-lg p-4 border-2 border-dashed border-primary/20">
                {isMatching && slotMachineUser ? (
                    <div className="w-full h-full flex items-center justify-center overflow-hidden relative">
                    <div className="absolute inset-0 bg-gradient-to-t from-card via-transparent to-card z-10" />
                    <div className="text-center animate-pulse" style={{ filter: 'blur(4px)' }}>
                        <img 
                            src={getProfilePictureUrl(slotMachineUser.profile_picture_id)} 
                            alt={slotMachineUser.Name}
                            className="w-24 h-24 rounded-full object-cover border-4 border-background shadow-lg mx-auto mb-4"
                        />
                        <h3 className="text-xl font-dancing text-romantic">{slotMachineUser.Name}</h3>
                        <p className="text-sm text-muted-foreground">{slotMachineUser.which_class}</p>
                    </div>
                    <div className="absolute inset-0 bg-gradient-to-b from-card via-transparent to-card z-10" />
                    </div>
                ) : matchedProfile ? (
                    <div className="w-full max-w-sm animate-fade-in">
                        <UserCard profile={matchedProfile} />
                    </div>
                ) : (
                    <div className="text-center text-muted-foreground">
                    <Heart className="w-16 h-16 mx-auto mb-4 opacity-30" />
                    <p className="font-dancing text-xl">Waiting for your match...</p>
                    </div>
                )}
                </div>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
            <div className="lg:col-span-2">
              <Card className="confession-card">
                <CardHeader>
                  <div className="flex items-center gap-4">
                    <Button variant="ghost" size="sm" onClick={() => setShowChat(false)}>
                      <ArrowLeft className="w-4 h-4" />
                    </Button>
                    <CardTitle className="flex items-center gap-3 text-xl font-dancing text-romantic">
                       Chat with {matchedProfile?.Name}
                    </CardTitle>
                  </div>
                </CardHeader>
                <CardContent>
                  <div className="h-64 overflow-y-auto border rounded-lg p-4 mb-4 space-y-2">
                    {chatMessages.length === 0 ? (
                      <p className="text-center text-muted-foreground font-dancing">
                        Start the conversation! 💕
                      </p>
                    ) : (
                      chatMessages.map((message, index) => (
                        <div key={index} className="animate-fade-in">
                          <p className={`p-2 rounded-lg ${
                            message.startsWith('You:') 
                              ? 'bg-gradient-romantic text-white ml-8' 
                              : 'bg-gradient-love text-romantic-dark mr-8'
                          }`}>
                            {message}
                          </p>
                        </div>
                      ))
                    )}
                  </div>

                  <div className="space-y-2">
                    <h4 className="font-semibold text-romantic">Quick Messages:</h4>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                      {prewrittenMessages.map((message, index) => (
                        <Button
                          key={index}
                          variant="outline"
                          size="sm"
                          onClick={() => sendMessage(message)}
                          className="text-left h-auto p-2 text-xs"
                        >
                          {message}
                        </Button>
                      ))}
                    </div>
                  </div>
                </CardContent>
              </Card>
            </div>

            <Card className="confession-card">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-xl font-dancing text-romantic">
                  <Sparkles className="w-5 h-5" />
                  Icebreaker Cards
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {icebreakerCards.map((card, index) => (
                  <div
                    key={index}
                    className="p-3 bg-gradient-love rounded-lg cursor-pointer hover:scale-105 transition-all duration-300 group"
                    onClick={() => sendIcebreaker(card)}
                  >
                    <p className="text-sm text-romantic-dark font-medium">{card}</p>
                    <div className="flex justify-end mt-2">
                      <Send className="w-4 h-4 text-romantic group-hover:translate-x-1 transition-transform" />
                    </div>
                  </div>
                ))}
              </CardContent>
            </Card>
          </div>
        )}
      </div>
    </div>
  );
};
