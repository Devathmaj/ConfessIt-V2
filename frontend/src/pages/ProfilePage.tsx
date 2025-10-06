import { useState, useEffect, useRef, useMemo } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { 
  User, 
  Save,
  Upload,
  Heart,
  Bell,
  Palette,
  Mail,
  AlertCircle,
  X
} from 'lucide-react';
import { toast } from 'sonner';
import { updateUserProfile, uploadProfilePicture } from '../services/api';
import { ModeToggle } from '@/components/ui/ModeToggle';
import { Navigation } from '@/components/Navigation';
import { isEqual } from 'lodash';
import { resolveProfilePictureUrl } from '@/lib/utils';

// Used for the emoji selector
const avatarOptions = ['💕', '🌟', '🎨', '🌺', '🦋', '✨', '🌸', '💖', '🌙', '🎵'];

// Used for the interest checklist
const interestOptions = [
  'Music', 'Movies', 'Reading', 'Gaming', 'Traveling', 'Cooking', 
  'Sports', 'Art', 'Photography', 'Dancing', 'Writing', 'Coding'
];

export const ProfilePage = () => {
  const { user, token, fetchAndSetUser } = useAuth();

  // State for form fields
  const [emoji, setEmoji] = useState(user?.emoji || '💕');
  const [bio, setBio] = useState(user?.bio || '');
  const [interests, setInterests] = useState<string[]>(user?.interests || []);
  const [isMatchmaking, setIsMatchmaking] = useState(user?.isMatchmaking || false);
  const [isNotifications, setIsNotifications] = useState(user?.isNotifications || false);
  const [isLovenotesRecieve, setIsLovenotesRecieve] = useState(user?.isLovenotesRecieve ?? true);
  
  // State for UI elements
  const [profilePictureUrl, setProfilePictureUrl] = useState<string | null>(null);
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Memoize the initial state of the user profile from AuthContext
  const initialProfileState = useMemo(() => {
    if (!user) return null;
    return {
      emoji: user.emoji || '💕',
      bio: user.bio || '',
      interests: user.interests || [],
      isMatchmaking: user.isMatchmaking || false,
      isNotifications: user.isNotifications || false,
      isLovenotesRecieve: user.isLovenotesRecieve ?? true,
    };
  }, [user]);

  // Effect to sync local state when the user context changes
  useEffect(() => {
    if (user) {
      setEmoji(user.emoji || '💕');
      setBio(user.bio || '');
      setInterests(user.interests || []);
      setIsMatchmaking(user.isMatchmaking || false);
      setIsNotifications(user.isNotifications || false);
      setIsLovenotesRecieve(user.isLovenotesRecieve ?? true);
      setProfilePictureUrl(resolveProfilePictureUrl(user.profile_picture_id));
    } else {
      setProfilePictureUrl(resolveProfilePictureUrl(null));
    }
  }, [user]);

  // Effect to detect unsaved changes by comparing current state with the initial state
  useEffect(() => {
    if (!initialProfileState) return;

    const currentState = {
      emoji,
      bio,
      interests,
      isMatchmaking,
      isNotifications,
      isLovenotesRecieve,
    };
    
    // Use lodash's isEqual for a deep comparison, especially for the interests array
    setHasUnsavedChanges(!isEqual(initialProfileState, currentState));
  }, [emoji, bio, interests, isMatchmaking, isNotifications, isLovenotesRecieve, initialProfileState]);

  const handleInterestChange = (interest: string) => {
    setInterests(prev => 
      prev.includes(interest) 
        ? prev.filter(i => i !== interest) 
        : [...prev, interest]
    );
  };

  const handleProfilePictureUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;

    if (file.size > 2 * 1024 * 1024) { // 2MB limit
      toast.error('File is too large. Maximum size is 2MB.');
      return;
    }

    try {
      await uploadProfilePicture(file);
      toast.success('Profile picture updated successfully! 📸');
      if(token) {
        await fetchAndSetUser(token);
      }
    } catch (error) {
      toast.error('Failed to upload profile picture.');
      console.error(error);
    }
  };

  const handleSaveProfile = async () => {
    const updatedDetails = {
      emoji,
      bio,
      interests,
      isMatchmaking,
      isNotifications,
      isLovenotesRecieve,
    };

    try {
      await updateUserProfile(updatedDetails);
      toast.success('Profile updated successfully! 💕');
      if(token) {
        await fetchAndSetUser(token);
      }
      setHasUnsavedChanges(false); // Reset unsaved changes flag
    } catch (error) {
      toast.error('Failed to update profile.');
      console.error(error);
    }
  };

  const handleDiscardChanges = () => {
    if (initialProfileState) {
      setEmoji(initialProfileState.emoji);
      setBio(initialProfileState.bio);
      setInterests(initialProfileState.interests);
      setIsMatchmaking(initialProfileState.isMatchmaking);
      setIsNotifications(initialProfileState.isNotifications);
      setIsLovenotesRecieve(initialProfileState.isLovenotesRecieve);
      toast.info('Changes have been discarded.');
    }
  };

  return (
    <div className="min-h-screen p-4 pt-24">
      <Navigation />
      <div className="max-w-4xl mx-auto pb-24"> {/* Added padding-bottom */}
        <div className="text-center mb-8">
            <h1 className="text-5xl font-dancing text-romantic mb-2">
                Profile
            </h1>
            <p className="text-xl text-muted-foreground">
                View and customize your profile
            </p>
        </div>

        <Card className="confession-card">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-2xl font-dancing text-romantic">
              <User className="w-6 h-6" />
              Your Details
            </CardTitle>
            <CardDescription>Manage your public and private information</CardDescription>
          </CardHeader>
          <CardContent className="space-y-8">
            {/* Profile Picture Section */}
            <div className="flex flex-col items-center gap-4">
                <div className="relative">
                    <div className="w-32 h-32 rounded-full p-1 bg-secondary">
                        <img
                            src={profilePictureUrl || `https://placehold.co/128x128/EED8E6/AF4D98?text=${user?.Name?.[0] || 'U'}`}
                            alt="Profile"
                            className="w-full h-full rounded-full object-cover"
                            onError={(e) => {
                                (e.target as HTMLImageElement).src = `https://placehold.co/128x128/EED8E6/AF4D98?text=${user?.Name?.[0] || 'U'}`;
                            }}
                        />
                    </div>
                    <Button 
                        size="icon" 
                        className="absolute bottom-0 right-0 rounded-full bg-gradient-romantic"
                        onClick={() => fileInputRef.current?.click()}
                    >
                        <Upload className="w-5 h-5" />
                    </Button>
                    <Input 
                        type="file" 
                        ref={fileInputRef} 
                        className="hidden" 
                        accept="image/png, image/jpeg, image/gif"
                        onChange={handleProfilePictureUpload}
                    />
                </div>
            </div>

            {/* Non-Editable Info */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <Label className="text-romantic font-semibold">Name</Label>
                <Input value={user?.Name || ''} disabled className="bg-muted" />
              </div>
              <div>
                <Label className="text-romantic font-semibold">Registration No.</Label>
                <Input value={user?.Regno || ''} disabled className="bg-muted" />
              </div>
              <div>
                <Label className="text-romantic font-semibold">Email</Label>
                <Input value={user?.email || ''} disabled className="bg-muted" />
              </div>
              <div>
                <Label className="text-romantic font-semibold">Class</Label>
                <Input value={user?.which_class || ''} disabled className="bg-muted" />
              </div>
               <div>
                <Label className="text-romantic font-semibold">Gender</Label>
                <Input value={user?.gender || ''} disabled className="bg-muted" />
              </div>
            </div>

            {/* Emoji Selection */}
            <div>
              <Label className="text-romantic font-semibold">Profile Emoji</Label>
              <div className="grid grid-cols-5 sm:grid-cols-10 gap-2 mt-2">
                {avatarOptions.map((option) => (
                  <button
                    key={option}
                    onClick={() => setEmoji(option)}
                    className={`w-12 h-12 text-2xl rounded-lg border-2 transition-all duration-300 hover:scale-110 ${
                      emoji === option 
                        ? 'border-romantic bg-gradient-love' 
                        : 'border-muted hover:border-romantic'
                    }`}
                  >
                    {option}
                  </button>
                ))}
              </div>
            </div>

            {/* Bio */}
            <div>
              <Label htmlFor="bio" className="text-romantic font-semibold">Bio</Label>
              <Textarea 
                id="bio"
                value={bio}
                onChange={(e) => setBio(e.target.value)}
                maxLength={30}
                placeholder="Tell us a little about yourself..."
                className="mt-1"
              />
              <p className="text-xs text-muted-foreground mt-1 text-right">{bio.length}/30</p>
            </div>

            {/* Interests */}
            <div>
              <Label className="text-romantic font-semibold">Interests</Label>
              <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2 mt-2">
                {interestOptions.map((interest) => (
                  <div key={interest} className="flex items-center">
                    <input 
                      type="checkbox"
                      id={`interest-${interest}`}
                      checked={interests.includes(interest)}
                      onChange={() => handleInterestChange(interest)}
                      className="h-4 w-4 rounded border-border text-romantic focus:ring-romantic"
                    />
                    <label htmlFor={`interest-${interest}`} className="ml-2 block text-sm">
                      {interest}
                    </label>
                  </div>
                ))}
              </div>
            </div>

            {/* Toggles */}
            <div className="space-y-4">
               <div className="flex items-center justify-between">
                <div className="space-y-0.5">
                  <div className="flex items-center gap-2">
                    <Heart className="w-4 h-4 text-romantic" />
                    <Label className="text-romantic font-semibold">Matchmaking</Label>
                  </div>
                  <p className="text-sm text-muted-foreground">
                    Enable random matchmaking feature
                  </p>
                </div>
                <Switch 
                  checked={isMatchmaking} 
                  onCheckedChange={setIsMatchmaking} 
                />
              </div>
              <div className="flex items-center justify-between">
                <div className="space-y-0.5">
                  <div className="flex items-center gap-2">
                    <Mail className="w-4 h-4 text-romantic" />
                    <Label className="text-romantic font-semibold">Love Notes</Label>
                  </div>
                  <p className="text-sm text-muted-foreground">
                    Enable receiving Love Notes from others
                  </p>
                </div>
                <Switch 
                  checked={isLovenotesRecieve} 
                  onCheckedChange={setIsLovenotesRecieve} 
                />
              </div>
              <div className="flex items-center justify-between">
                <div className="space-y-0.5">
                  <div className="flex items-center gap-2">
                    <Bell className="w-4 h-4 text-romantic" />
                    <Label className="text-romantic font-semibold">Notifications</Label>
                  </div>
                  <p className="text-sm text-muted-foreground">
                    Receive notifications for matches and messages
                  </p>
                </div>
                <Switch 
                  checked={isNotifications} 
                  onCheckedChange={setIsNotifications} 
                />
              </div>
              <div className="flex items-center justify-between">
                <div className="space-y-0.5">
                  <div className="flex items-center gap-2">
                    <Palette className="w-4 h-4 text-romantic" />
                    <Label className="text-romantic font-semibold">Theme</Label>
                  </div>
                  <p className="text-sm text-muted-foreground">
                    Toggle between light and dark mode
                  </p>
                </div>
                <ModeToggle />
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Unsaved Changes Indicator and Action Buttons */}
      {hasUnsavedChanges && (
        <div className="fixed bottom-0 left-0 right-0 z-50">
          <div className="container max-w-4xl mx-auto p-4">
            <div className="bg-background/95 p-4 rounded-lg shadow-lg border border-border flex flex-col sm:flex-row items-center justify-between gap-4">
              <div className="flex items-center gap-2 text-amber-500">
                <AlertCircle className="w-5 h-5" />
                <p className="font-semibold">You have unsaved changes!</p>
              </div>
              <div className="flex items-center gap-4">
                <Button 
                  onClick={handleDiscardChanges}
                  variant="outline"
                  className="w-full sm:w-auto"
                >
                  <X className="w-4 h-4 mr-2" />
                  Discard
                </Button>
                <Button 
                  onClick={handleSaveProfile}
                  className="w-full sm:w-auto bg-gradient-romantic hover:opacity-90 text-white font-dancing text-lg"
                >
                  <Save className="w-5 h-5 mr-2" />
                  Save Changes
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
