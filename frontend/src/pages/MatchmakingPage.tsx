// src/pages/MatchmakingPage.tsx

import { useState, useEffect, useCallback } from 'react';
import { useAuth, User } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { CountdownTimer } from '@/components/ui/countdown-timer';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { findMatch, checkMatchmakingStatus, requestConversation, getCurrentConversation } from '@/services/api';
import { resolveProfilePictureUrl } from '@/lib/utils';
import { ConversationDialog } from '@/components/ConversationDialog';
import {
  Heart,
  MessageCircle,
  Sparkles,
  ArrowLeft,
  Send,
  Clock,
} from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';
import {
    AlertDialog,
    AlertDialogAction,
    AlertDialogCancel,
    AlertDialogContent,
    AlertDialogDescription,
    AlertDialogFooter,
    AlertDialogHeader,
    AlertDialogTitle,
} from "@/components/ui/alert-dialog";

// Pre-written messages for quick chat options
const prewrittenMessages = [
  "Hey! I love your profile, would you like to chat? 👋",
  "Your interests caught my eye! What's your favorite book? 📚",
  "Coffee lover here too! Know any good spots? ☕",
  "Your bio made me smile! Tell me more about yourself ✨"
];

// Icebreaker questions to start conversations
const icebreakerCards = [
  "If you could have dinner with anyone, who would it be?",
  "What's your idea of a perfect day?",
  "Share your most embarrassing moment!",
  "What's on your bucket list?",
  "Describe yourself in three emojis!"
];

// Mock data for the "slot machine" animation while matching
const mockMatches: User[] = [
    { id: 'mock1', Name: 'Alex', Regno: '123', emoji: '😊', username: 'alex', which_class: 'CSE', profile_picture_id: 'https://placehold.co/96x96/fecdd3/be123c?text=A', interests: ['Gaming', 'Music'], bio: 'Just a mock user.' },
    { id: 'mock2', Name: 'Jordan', Regno: '456', emoji: '😎', username: 'jordan', which_class: 'ECE', profile_picture_id: 'https://placehold.co/96x96/fecdd3/be123c?text=J', interests: ['Sports', 'Movies'], bio: 'Another mock user.' },
    { id: 'mock3', Name: 'Taylor', Regno: '789', emoji: '🤩', username: 'taylor', which_class: 'MECH', profile_picture_id: 'https://placehold.co/96x96/fecdd3/be123c?text=T', interests: ['Reading', 'Hiking'], bio: 'A third mock user.' },
];

/**
 * Renders a glass-like heart SVG that fills with a liquid animation.
 * @param {object} props - The component props.
 * @param {number} props.fillPercentage - The percentage to fill the heart (0-100).
 * @returns {JSX.Element} The GlassHeart component.
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
            </svg>
        </div>
    );
};

/**
 * A component that displays a countdown timer until a specified expiry timestamp.
 * @param {object} props - The component props.
 * @param {string} props.expiryTimestamp - The ISO string of the expiration date.
 * @returns {JSX.Element} The MatchExpiryTimer component.
 */
const MatchExpiryTimer = ({ expiryTimestamp }: { expiryTimestamp: string }) => {
    const calculateTimeLeft = useCallback(() => {
        const utcTimestamp = expiryTimestamp.endsWith('Z') ? expiryTimestamp : `${expiryTimestamp}Z`;
        const difference = +new Date(utcTimestamp) - +new Date();
        let timeLeft = { hours: 0, minutes: 0, seconds: 0 };

        if (difference > 0) {
            timeLeft = {
                hours: Math.floor(difference / (1000 * 60 * 60)),
                minutes: Math.floor((difference / 1000 / 60) % 60),
                seconds: Math.floor((difference / 1000) % 60),
            };
        }
        return timeLeft;
    }, [expiryTimestamp]);

    const [timeLeft, setTimeLeft] = useState(calculateTimeLeft());

    useEffect(() => {
        const timer = setInterval(() => {
            setTimeLeft(calculateTimeLeft());
        }, 1000);

        return () => clearInterval(timer);
    }, [calculateTimeLeft]);

    const formatTime = (value: number) => value.toString().padStart(2, '0');

    return (
        <div className="text-center text-sm text-muted-foreground mt-3 flex items-center justify-center gap-2 font-mono">
            <Clock className="w-4 h-4" />
            {timeLeft.hours > 0 || timeLeft.minutes > 0 || timeLeft.seconds > 0 ? (
                <span>
                    Expires in: {formatTime(timeLeft.hours)}h {formatTime(timeLeft.minutes)}m {formatTime(timeLeft.seconds)}s
                </span>
            ) : (
                <span>Match expired!</span>
            )}
        </div>
    );
};

/**
 * The main component for the matchmaking page.
 * Handles matchmaking logic, UI state, and chat functionality.
 * @returns {JSX.Element} The MatchmakingPage component.
 */
export const MatchmakingPage = () => {
  const { user } = useAuth();
  const [isMatching, setIsMatching] = useState(false);
  const [matchedProfile, setMatchedProfile] = useState<User & { matchId?: string } | null>(null);
  const [slotMachineUser, setSlotMachineUser] = useState<User | null>(null);
  const [heartFill, setHeartFill] = useState(0);
  const [showChat, setShowChat] = useState(false);
  const [chatMessages, setChatMessages] = useState<string[]>([]);
  const [conversationStatus, setConversationStatus] = useState<'none' | 'pending' | 'requested' | 'accepted' | 'rejected'>('none');
  const [showConversationDialog, setShowConversationDialog] = useState(false);
  const [hasActiveConversation, setHasActiveConversation] = useState(false);
  const [expiresAt, setExpiresAt] = useState<string | null>(null);
  
  const [showConfirmationDialog, setShowConfirmationDialog] = useState(false);
  const [showCooldownDialog, setShowCooldownDialog] = useState(false);
  const [cooldownMessage, setCooldownMessage] = useState("");
  const [showRareMatchDialog, setShowRareMatchDialog] = useState(false);

  /**
   * Constructs the full URL for a user's profile picture.
   * @param {string} [pictureId] - The ID or URL of the picture.
   * @returns {string} The full URL to the profile picture.
   */
  const getProfilePictureUrl = (pictureId?: string | null) => resolveProfilePictureUrl(pictureId ?? null);

  /**
   * Fetches the user's current matchmaking status from the server.
   * Used to check if the user already has a match upon loading the page.
   */
  const fetchMatchmakingStatus = useCallback(async () => {
    try {
      const response = await checkMatchmakingStatus();
      console.log("fetchMatchmakingStatus response:", response); // Debug log
      if (response.status === 'matched') {
        const now = new Date();
        const utcTimestamp = response.expires_at.endsWith('Z') ? response.expires_at : `${response.expires_at}Z`;
        if (now >= new Date(utcTimestamp)) {
          // Match is expired, don't set it
          return;
        }
        console.log("Setting matched profile with:", { ...response.matched_with, matchId: response.match_id }); // Debug log
        setMatchedProfile({ ...response.matched_with, matchId: response.match_id });
        setExpiresAt(response.expires_at);
        
        // Check if there's already a conversation for this match
        checkForExistingConversation(response.match_id);
      }
    } catch (error: any) {
      console.error("Failed to check matchmaking status on load:", error.response?.data?.detail || error.message);
    }
  }, []);

    const checkForExistingConversation = async (matchId?: string) => {
    try {
      const response = await getCurrentConversation();
      if (response.status === 'success') {
        if (!response.match.is_expired) {
          // Only show the matched profile if not expired
          setMatchedProfile({
            ...response.other_user,
            matchId: response.match.id
          });
          setExpiresAt(response.match.expires_at);
          setConversationStatus(response.conversation.status);
          setHasActiveConversation(response.conversation.status === 'accepted');
        } else {
          // Match is expired, don't show it
          setHasActiveConversation(false);
          setConversationStatus('none');
          toast.info(`Your previous match has expired. You can find a new match in ${getTimeUntilNextMatch(response.match.expires_at)}`);
        }
      } else {
        setHasActiveConversation(false);
        setConversationStatus('none');
      }
    } catch (error: any) {
      console.error('Failed to check for existing conversation:', error);
      setHasActiveConversation(false);
      setConversationStatus('none');
    }
  };

  const getTimeUntilNextMatch = (expiresAt: string) => {
    const now = new Date();
    const expiresUTC = new Date(expiresAt + 'Z').getTime();
    const nowUTC = now.getTime();
    
    // If match hasn't expired yet, show time until expiry
    if (nowUTC < expiresUTC) {
      const timeUntilExpiry = expiresUTC - nowUTC;
      const minutes = Math.floor(timeUntilExpiry / (1000 * 60));
      if (minutes < 60) return `${minutes}m`;
      
      const hours = Math.floor(minutes / 60);
      const remainingMinutes = minutes % 60;
      return `${hours}h ${remainingMinutes}m`;
    }
    
    // If match has expired, add 1 hour cooldown
    const cooldownEnd = expiresUTC + (60 * 60 * 1000); // 1 hour after expiry
    const timeUntilCooldown = cooldownEnd - nowUTC;
    
    if (timeUntilCooldown <= 0) return 'now';
    
    const minutes = Math.floor(timeUntilCooldown / (1000 * 60));
    if (minutes < 60) return `${minutes}m`;
    
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
  };

  const refreshConversationStatus = async () => {
    if (matchedProfile?.matchId) {
      await checkForExistingConversation(matchedProfile.matchId);
    }
  };

  const handleConversationAccepted = () => {
    setConversationStatus('accepted');
    setHasActiveConversation(true);
  };

  /**
   * Handles when the initiator clicks "Send Message Request"
   * Updates conversation from 'pending' to 'requested' and notifies receiver
   */
  const handleSendMessageRequest = async () => {
    console.log("matchedProfile:", matchedProfile);
    if (!matchedProfile?.matchId) {
        toast.error("Cannot send message request: Match ID is missing.");
        console.log("matchId is missing from:", matchedProfile);
        return;
    }
    try {
        console.log("Sending message request with matchId:", matchedProfile.matchId);
        await requestConversation(matchedProfile.matchId);
        toast.success(`Message request sent to ${matchedProfile.Name}!`);
        setConversationStatus('requested');
        await refreshConversationStatus(); // Refresh to get updated status
    } catch (error: any) {
        toast.error(error.response?.data?.detail || "Failed to send message request.");
    }
  };

  useEffect(() => {
    fetchMatchmakingStatus();
    // Don't show toast notifications on page load - users will see them in inbox
  }, [fetchMatchmakingStatus]);

  // Check for existing conversations on component mount
  useEffect(() => {
    const checkForExistingConversations = async () => {
      try {
        const response = await getCurrentConversation();
        if (response.status === 'success') {
          if (!response.match.is_expired) {
            // Only show the matched profile if not expired
            setMatchedProfile({
              ...response.other_user,
              matchId: response.match.id
            });
            setExpiresAt(response.match.expires_at);
            setConversationStatus(response.conversation.status);
            setHasActiveConversation(response.conversation.status === 'accepted');
          } else {
            // Match is expired, don't show it
            setHasActiveConversation(false);
            setConversationStatus('none');
            toast.info(`Your previous match has expired. You can find a new match in ${getTimeUntilNextMatch(response.match.expires_at)}`);
          }
        }
      } catch (error: any) {
        setHasActiveConversation(false);
      }
    };

    checkForExistingConversations();

    // Set up periodic check for expired conversations (every minute)
    const interval = setInterval(async () => {
      // Check for match expiry first
      if (expiresAt) {
        const now = new Date();
        const utcTimestamp = expiresAt.endsWith('Z') ? expiresAt : `${expiresAt}Z`;
        if (now >= new Date(utcTimestamp)) {
          setMatchedProfile(null);
          setExpiresAt(null);
          setConversationStatus('none');
          setHasActiveConversation(false);
          toast.info("Your match has expired. You can find a new match now!");
          return; // No need to check conversation if match expired
        }
      }

      // Check for conversation expiry if active
      if (hasActiveConversation && matchedProfile?.matchId) {
        try {
          const response = await getCurrentConversation();
          if (response.status === 'success' && response.match.is_expired) {
            setHasActiveConversation(false);
            setConversationStatus('none');
            toast.info(`Your match has expired. You can find a new match in ${getTimeUntilNextMatch(response.match.expires_at)}`);
          }
        } catch (error) {
          console.error('Failed to check conversation expiry:', error);
        }
      }
    }, 60000); // Check every minute

    return () => clearInterval(interval);
  }, [hasActiveConversation, matchedProfile?.matchId]);

  /**
   * Handles the click event on the main heart icon.
   * Checks the user's eligibility and either shows a confirmation dialog
   * or a cooldown message if they've matched recently.
   */
  const handleHeartClick = async () => {
    if (isMatching || matchedProfile) return;

    try {
      const response = await checkMatchmakingStatus();
      console.log("handleHeartClick response:", response); // Debug log
      if (response.status === 'eligible') {
        setShowConfirmationDialog(true);
      } else if (response.status === 'matched') {
        const now = new Date();
        const utcTimestamp = response.expires_at.endsWith('Z') ? response.expires_at : `${response.expires_at}Z`;
        if (now >= new Date(utcTimestamp)) {
          // Match is expired, don't set it
          return;
        }
        console.log("Setting matched profile from handleHeartClick:", { ...response.matched_with, matchId: response.match_id }); // Debug log
        setMatchedProfile({ ...response.matched_with, matchId: response.match_id });
        setExpiresAt(response.expires_at);
        toast.info("You already have a match!");
      }
    } catch (error: any) {
      if (error.response?.status === 429) {
        setCooldownMessage(error.response.data.detail);
        setShowCooldownDialog(true);
      } else {
        toast.error("An error occurred. Please try again later.");
      }
    }
  };

  /**
   * Initiates the matchmaking process after user confirmation.
   * Triggers animations and calls the API to find a match.
   */
  const startMatching = async () => {
    setShowConfirmationDialog(false);
    setIsMatching(true);
    setMatchedProfile(null);
    setHeartFill(0);
    setShowChat(false);
    setChatMessages([]);
    setExpiresAt(null);

    const slotInterval = setInterval(() => {
      const randomIndex = Math.floor(Math.random() * mockMatches.length);
      setSlotMachineUser(mockMatches[randomIndex]);
    }, 100);

    const totalDuration = 5000;
    const intervalDuration = 50;
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
              // The backend now returns { matched_with, match_id, expires_at, is_rare_match }
              // and automatically creates a pending conversation
              const newMatchedProfile = { 
                ...finalMatch.matched_with, 
                matchId: finalMatch.match_id 
              };
              setMatchedProfile(newMatchedProfile);
              setExpiresAt(finalMatch.expires_at);
              setConversationStatus('pending'); // Automatically created as pending
              setHasActiveConversation(false); // Not active until receiver accepts
              
              // Check if this is a rare same-gender match
              if (finalMatch.is_rare_match) {
                setShowRareMatchDialog(true);
              } else {
                toast.success('Match found! 💕');
              }
              
              // Immediately show the matched profile
              console.log('Setting matched profile:', newMatchedProfile);
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

  /**
   * Sends a message in the chat and simulates a reply.
   * @param {string} message - The message to send.
   */
  const sendMessage = (message: string) => {
    setChatMessages(prev => [...prev, `You: ${message}`]);
    
    setTimeout(() => {
      const responses = ["That's so sweet! 💕", "I'd love to chat more!", "You seem really interesting!"];
      const response = responses[Math.floor(Math.random() * responses.length)];
      setChatMessages(prev => [...prev, `${matchedProfile?.Name}: ${response}`]);
    }, 1000);
  };

  /**
   * A component to display a user's profile card.
   * @param {object} props - The component props.
   * @param {User} props.profile - The user profile data to display.
   * @returns {JSX.Element} The UserCard component.
   */
  const UserCard = ({ profile }: { profile: User }) => (
    <Card className="confession-card">
      <CardHeader className="items-center text-center">
        <img 
            src={getProfilePictureUrl(profile.profile_picture_id)} 
            alt={profile.Name}
            className="w-24 h-24 rounded-full object-cover border-4 border-background shadow-lg"
        />
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
          <p className="text-sm text-muted-foreground">"{profile.bio || 'No bio yet!'}"</p>
        </div>
      </CardContent>
    </Card>
  );

  return (
    <div className="min-h-screen p-4 pt-24 relative overflow-hidden">
      <Navigation />
      <FloatingHearts />
      
      <div className="max-w-7xl mx-auto mb-8 px-4">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-6">
          <div className="text-center lg:text-left">
            <h1 className="text-2xl sm:text-3xl md:text-4xl lg:text-5xl font-dancing text-romantic mb-2 break-words">Random Matchmaking 💕</h1>
            <p className="text-sm sm:text-base md:text-xl text-muted-foreground break-words">Find your perfect Valentine match!</p>
          </div>
          <CountdownTimer />
        </div>
      </div>

      <div className="max-w-7xl mx-auto">
        {!showChat ? (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-8 items-center">
            <div className="min-h-[400px] flex items-center justify-center bg-card rounded-lg p-4 border-2 border-dashed border-primary/20">
                <div className="w-full max-w-sm">{user && <UserCard profile={user} />}</div>
            </div>

            <div className="flex flex-col items-center justify-center space-y-6 h-full">
              {!matchedProfile && (
                <div className="relative w-48 h-48 cursor-pointer group" onClick={handleHeartClick}>
                  <GlassHeart fillPercentage={heartFill} />
                </div>
              )}

              {!isMatching && !matchedProfile && (
                <div className="text-center space-y-4">
                  <h2 className="text-2xl font-dancing text-romantic">Click the heart to find your match! 💕</h2>
                  <p className="text-muted-foreground">Find someone special to connect with!</p>
                </div>
              )}

              {matchedProfile && hasActiveConversation && (
                <div className="text-center space-y-4">
                  <div className="text-4xl">💬</div>
                  <h2 className="text-2xl font-dancing text-romantic">Conversation Active!</h2>
                  <p className="text-muted-foreground">You have an active conversation with {matchedProfile.Name}</p>
                  <Button 
                    onClick={() => setShowConversationDialog(true)}
                    className="bg-gradient-to-r from-pink-500 to-rose-500 hover:from-pink-600 hover:to-rose-600 text-white"
                    size="lg"
                  >
                    Open Conversation
                  </Button>
                  {expiresAt && (
                    <div className="text-sm text-muted-foreground">
                      <Clock className="w-4 h-4 inline mr-2" />
                      Time until next match: {getTimeUntilNextMatch(expiresAt)}
                    </div>
                  )}
                </div>
              )}

              {matchedProfile && conversationStatus === 'requested' && (
                <div className="text-center space-y-4">
                  <div className="text-4xl">⏳</div>
                  <h2 className="text-2xl font-dancing text-romantic">Waiting for Response</h2>
                  <p className="text-muted-foreground">Waiting for {matchedProfile.Name} to accept your message request</p>
                  {expiresAt && (
                    <div className="text-sm text-muted-foreground">
                      <Clock className="w-4 h-4 inline mr-2" />
                      Time until next match: {getTimeUntilNextMatch(expiresAt)}
                    </div>
                  )}
                </div>
              )}

              {matchedProfile && conversationStatus === 'rejected' && (
                <div className="text-center space-y-4">
                  <div className="text-4xl">❌</div>
                  <h2 className="text-2xl font-dancing text-romantic">Message Request Rejected</h2>
                  <p className="text-muted-foreground">{matchedProfile.Name} rejected your message request</p>
                  {expiresAt && (
                    <div className="text-sm text-muted-foreground">
                      <Clock className="w-4 h-4 inline mr-2" />
                      Time until next match: {getTimeUntilNextMatch(expiresAt)}
                    </div>
                  )}
                </div>
              )}

              {matchedProfile && conversationStatus === 'pending' && (
                <div className="text-center space-y-4">
                  <div className="text-4xl">💕</div>
                  <h2 className="text-2xl font-dancing text-romantic">Match Found!</h2>
                  <p className="text-muted-foreground">You matched with {matchedProfile.Name}!</p>
                  <Button 
                    onClick={handleSendMessageRequest}
                    className="bg-gradient-to-r from-pink-500 to-rose-500 hover:from-pink-600 hover:to-rose-600 text-white"
                    size="lg"
                  >
                    Send Message Request
                  </Button>
                  {expiresAt && (
                    <div className="text-sm text-muted-foreground">
                      <Clock className="w-4 h-4 inline mr-2" />
                      Time until next match: {getTimeUntilNextMatch(expiresAt)}
                    </div>
                  )}
                </div>
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
                    {conversationStatus === 'accepted' && (
                      <div className="text-center space-y-4">
                        <div className="text-2xl">💬</div>
                        <h3 className="text-xl font-semibold text-romantic">Conversation Active!</h3>
                        <p className="text-muted-foreground">You can now chat with your match!</p>
                        <Button 
                          onClick={() => setShowConversationDialog(true)}
                          className="bg-gradient-to-r from-pink-500 to-rose-500 hover:from-pink-600 hover:to-rose-600"
                        >
                          Open Chat
                        </Button>
                      </div>
                    )}

                    {conversationStatus === 'requested' && (
                      <div className="text-center space-y-4">
                        <div className="text-2xl">⏳</div>
                        <h3 className="text-xl font-semibold text-romantic">Waiting for Response</h3>
                        <p className="text-muted-foreground">Waiting for your match to accept the message request.</p>
                      </div>
                    )}

                    {conversationStatus === 'pending' && (
                      <div className="text-center space-y-4">
                        <div className="text-2xl">💕</div>
                        <h3 className="text-xl font-semibold text-romantic">Send a Message Request!</h3>
                        <p className="text-muted-foreground">Click "Send Message Request" to start a conversation with {matchedProfile?.Name}.</p>
                      </div>
                    )}

                    {conversationStatus === 'rejected' && (
                      <div className="text-center space-y-4">
                        <div className="text-2xl">❌</div>
                        <h3 className="text-xl font-semibold text-romantic">Request Rejected</h3>
                        <p className="text-muted-foreground">Your match rejected the message request.</p>
                      </div>
                    )}

                    {conversationStatus === 'accepted' && chatMessages.length === 0 && (
                        <p className="text-center text-muted-foreground font-dancing">Start the conversation! 💕</p>
                    )}
                    {conversationStatus === 'accepted' && chatMessages.map((message, index) => (
                        <div key={index} className="text-sm">
                            <span className="font-semibold text-romantic">{message}</span>
                        </div>
                    ))}
                  </div>

                  <div className="space-y-2">
                    <h4 className="font-semibold text-romantic">Quick Messages:</h4>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                      {prewrittenMessages.map((message, index) => (
                        <Button key={index} variant="outline" size="sm" onClick={() => sendMessage(message)} className="text-left h-auto p-2 text-xs" disabled={conversationStatus !== 'accepted'}>
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
                  <div key={index} className={`p-3 bg-gradient-love rounded-lg ${conversationStatus === 'accepted' ? 'cursor-pointer hover:scale-105' : 'opacity-50 cursor-not-allowed'} transition-all duration-300 group`} onClick={() => conversationStatus === 'accepted' && sendMessage(card)}>
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

      <AlertDialog open={showConfirmationDialog} onOpenChange={setShowConfirmationDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Confirm Matchmaking</AlertDialogTitle>
            <AlertDialogDescription>
              You can only find a new match once every 4 hours. Are you sure you want to use your attempt now?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={startMatching}>Proceed</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={showCooldownDialog} onOpenChange={setShowCooldownDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Matchmaking on Cooldown</AlertDialogTitle>
            <AlertDialogDescription>{cooldownMessage}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogAction onClick={() => setShowCooldownDialog(false)}>OK</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Rare Same-Gender Match Dialog */}
      <AlertDialog open={showRareMatchDialog} onOpenChange={setShowRareMatchDialog}>
        <AlertDialogContent className="max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-2xl font-bold text-center text-romantic">
              ✨ 🎉 🌟 Matrix Glitch Detected! 🌟 🎉 ✨
            </AlertDialogTitle>
            <AlertDialogDescription className="text-center space-y-4 py-4">
              <div className="text-lg font-semibold">
                You found the ultra-rare glitch in the Matrix!
              </div>
              <div className="text-base">
                You had a <span className="font-bold text-romantic">0.1% chance</span> of matching with the same gender — this almost never happens!
              </div>
              <div className="text-xl font-bold animate-pulse">
                🌈 You broke the algorithm! 🌈
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogAction onClick={() => setShowRareMatchDialog(false)}>
              Continue
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Conversation Dialog */}
      {showConversationDialog && (
        <ConversationDialog
          onClose={() => setShowConversationDialog(false)}
          onRefresh={refreshConversationStatus}
          onConversationAccepted={handleConversationAccepted}
        />
      )}
    </div>
  );
};