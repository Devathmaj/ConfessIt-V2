import { useState, useEffect, useRef } from 'react';
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
  Palette
} from 'lucide-react';
import { toast } from 'sonner';
import { updateUserProfile, uploadProfilePicture } from '../services/api';
import { ModeToggle } from '@/components/ui/ModeToggle';
import { Navigation } from '@/components/Navigation';

// Used for the emoji selector
const avatarOptions = ['💕', '🌟', '🎨', '🌺', '🦋', '✨', '🌸', '💖', '🌙', '🎵'];

// Used for the interest checklist
const interestOptions = [
  'Music', 'Movies', 'Reading', 'Gaming', 'Traveling', 'Cooking', 
  'Sports', 'Art', 'Photography', 'Dancing', 'Writing', 'Coding'
];

export const ProfilePage = () => {
  const { user, token, fetchAndSetUser } = useAuth();
  const [emoji, setEmoji] = useState(user?.emoji || '💕');
  const [bio, setBio] = useState(user?.bio || '');
  const [interests, setInterests] = useState<string[]>(user?.interests || []);
  const [isMatchmaking, setIsMatchmaking] = useState(user?.isMatchmaking || false);
  const [isNotifications, setIsNotifications] = useState(user?.isNotifications || false);
  const [profilePictureUrl, setProfilePictureUrl] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // Used to sync component state with the user object from AuthContext
    if (user) {
      setEmoji(user.emoji || '💕');
      setBio(user.bio || '');
      setInterests(user.interests || []);
      setIsMatchmaking(user.isMatchmaking || false);
      setIsNotifications(user.isNotifications || false);
      if (user.profile_picture_id) {
        // Corrected URL to fetch the profile picture
        setProfilePictureUrl(`http://localhost:8001/profile_pictures/${user.profile_picture_id}`);
      }
    }
  }, [user]);

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
      const response = await uploadProfilePicture(file);
      toast.success('Profile picture updated successfully! 📸');
      // Used to refresh user data to get the new picture ID
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
    };

    try {
      await updateUserProfile(updatedDetails);
      toast.success('Profile updated successfully! 💕');
       if(token) {
        await fetchAndSetUser(token);
      }
    } catch (error) {
      toast.error('Failed to update profile.');
      console.error(error);
    }
  };

  return (
    <div className="min-h-screen p-4 pt-24">
      <Navigation />
      <div className="max-w-4xl mx-auto">
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
                    {/* Used a theme-aware background color instead of hardcoded white */}
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
                <Switch checked={isMatchmaking} onCheckedChange={setIsMatchmaking} />
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
                <Switch checked={isNotifications} onCheckedChange={setIsNotifications} />
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

            <Button 
              onClick={handleSaveProfile}
              className="w-full bg-gradient-romantic hover:opacity-90 text-white font-dancing"
            >
              <Save className="w-4 h-4 mr-2" />
              Save Changes
            </Button>
          </CardContent>
        </Card>
      </div>
    </div>
  );
};
