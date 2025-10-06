import { useState, useEffect } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { 
  Heart, 
  MessageCircle, 
  Shuffle, 
  User,
  Mail, 
  Gamepad2,
  LogOut,
  ChevronRight,
  Info
} from 'lucide-react';
import { toast } from 'sonner';
import { getConfessions } from '../services/api';
import { resolveProfilePictureUrl } from '@/lib/utils';

// Import the new Navigation component
import { Navigation } from '@/components/Navigation';

// Import video and image assets
import DashboardWebm from '@/assets/Dashboard.webm';
import DashboardMp4 from '@/assets/Dashboard.mp4';
import DashboardWebp from '@/assets/Dashboard.webp';

// Interface for navigation items
interface NavigationItem {
  id: string;
  title: string;
  description: string;
  icon: React.ComponentType<any>;
}

// Interface for a confession
interface Confession {
  id: string;
  confession: string;
  is_anonymous: boolean;
  user_id: string;
  timestamp: string;
  comments: any[];
  heart_count: number;
  haha_count: number;
  whoa_count: number;
  heartbreak_count: number;
  comment_count: number;
  user_reaction: string | null;
}

// Data for the main navigation cards
const navigationItems: NavigationItem[] = [
  {
    id: 'confessions',
    title: 'Confessions',
    description: 'Share anonymously',
    icon: MessageCircle,
  },
  {
    id: 'matchmaking',
    title: 'Matchmaking',
    description: 'Find your match',
    icon: Shuffle,
  },
  {
    id: 'love-notes',
    title: 'Love Notes',
    description: 'Create digital cards',
    icon: Mail,
  },
  {
    id: 'mini-games',
    title: 'Mini-Games',
    description: 'Break the ice',
    icon: Gamepad2,
  }
];

export const UserDashboard = () => {
  const { user, logout } = useAuth();
  const [selectedPage, setSelectedPage] = useState<string | null>(null);
  const [profilePictureUrl, setProfilePictureUrl] = useState<string | null>(null);
  const [trendingConfessions, setTrendingConfessions] = useState<string[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    // Set profile picture URL if available
    setProfilePictureUrl(resolveProfilePictureUrl(user?.profile_picture_id ?? null));

    // Fetch trending confessions
    fetchTrendingConfessions();
  }, [user]);

  const fetchTrendingConfessions = async () => {
    try {
      setIsLoading(true);
      // Fetch confessions ordered by popularity
      const popularConfessions = await getConfessions('popularity');
      
      // Fetch confessions ordered by time (recent)
      const recentConfessions = await getConfessions('time');
      
      // Fetch confessions ordered by comments
      const commentedConfessions = await getConfessions('comments');
      
      // Select 4 unique trending confessions
      const selectedConfessions = [];
      
      // First two: most popular confessions
      if (popularConfessions.length > 0) {
        selectedConfessions.push(popularConfessions[0].confession);
        if (popularConfessions.length > 1) {
          selectedConfessions.push(popularConfessions[1].confession);
        }
      }
      
      // Third: most commented confession (if not already included)
      if (commentedConfessions.length > 0) {
        const mostCommented = commentedConfessions[0].confession;
        if (!selectedConfessions.includes(mostCommented)) {
          selectedConfessions.push(mostCommented);
        } else if (commentedConfessions.length > 1) {
          // If the most commented is already included, take the next one
          selectedConfessions.push(commentedConfessions[1].confession);
        }
      }
      
      // Fourth: most recent confession (if not already included)
      if (recentConfessions.length > 0) {
        const mostRecent = recentConfessions[0].confession;
        if (!selectedConfessions.includes(mostRecent)) {
          selectedConfessions.push(mostRecent);
        } else if (recentConfessions.length > 1) {
          // If the most recent is already included, take the next one
          selectedConfessions.push(recentConfessions[1].confession);
        } else if (commentedConfessions.length > 2) {
          // If no recent available, take another commented
          selectedConfessions.push(commentedConfessions[2].confession);
        }
      }
      
      // Fill with placeholders if we don't have enough confessions
      const placeholders = [
        "Someone out there thinks you're absolutely amazing ✨",
        "I've been crushing on you since the first day we met...",
        "Your smile is the brightest part of my day 😊",
        "I love how you laugh at your own jokes before telling them"
      ];
      
      while (selectedConfessions.length < 4) {
        const placeholder = placeholders[selectedConfessions.length];
        if (!selectedConfessions.includes(placeholder)) {
          selectedConfessions.push(placeholder);
        }
      }
      
      setTrendingConfessions(selectedConfessions.slice(0, 4));
    } catch (error) {
      console.error('Failed to fetch trending confessions:', error);
      // Use placeholders if API call fails
      setTrendingConfessions([
        "Someone out there thinks you're absolutely amazing ✨",
        "I've been crushing on you since the first day we met...",
        "Your smile is the brightest part of my day 😊",
        "I love how you laugh at your own jokes before telling them"
      ]);
    } finally {
      setIsLoading(false);
    }
  };

  /**
   * Used to handle navigation to different application pages.
   * Constructs the route and redirects the user.
   * @param {string} pageId - The identifier for the page to navigate to.
   */
  const handleNavigation = (pageId: string) => {
    setSelectedPage(pageId);
    const routes = {
      'confessions': '/confessions',
      'matchmaking': '/matchmaking',
      'love-notes': '/love-notes',
      'mini-games': '/mini-games',
      'profile': '/profile',
      'about': '/about'
    };
    
    const route = routes[pageId as keyof typeof routes];
    if (route) {
      window.location.href = route;
    } else {
      const pageTitle = navigationItems.find(item => item.id === pageId)?.title || 'Profile';
      toast.success(`Navigating to ${pageTitle} ✨`);
    }
  };

  /**
   * Used to log the user out of the application and display a farewell message.
   */
  const handleLogout = () => {
    logout();
    toast.success('Goodbye! Come back soon ❤️');
  };

  /**
   * A reusable card component with a refined glassmorphism style.
   * Features a subtle hover effect with a glowing radial gradient.
   */
  const GlassCard = ({ children, className, ...props }: { children: React.ReactNode, className?: string, [key: string]: any }) => (
    <div 
      className={`group relative bg-white/5 backdrop-blur-xl border border-white/10 rounded-xl shadow-lg transition-all duration-300 overflow-hidden hover:bg-white/10 hover:border-white/20 ${className}`}
      {...props}
    >
      <div className="absolute -inset-1 bg-gradient-to-r from-pink-600/10 to-purple-600/10 opacity-0 group-hover:opacity-100 transition-opacity duration-500"></div>
      <div className="relative z-10 h-full">
        {children}
      </div>
    </div>
  );

  return (
    <div className="min-h-screen w-full text-white font-sans overflow-hidden">
      {/* Add the Navigation component here */}
      <Navigation />

      {/* Background Video */}
      <div className="fixed top-0 left-0 w-full h-full overflow-hidden -z-10">
        <video 
          poster={DashboardWebp}
          autoPlay 
          loop 
          muted 
          playsInline
          className="w-full h-full object-cover"
        >
          <source src={DashboardWebm} type="video/webm" />
          <source src={DashboardMp4} type="video/mp4" />
          Your browser does not support the video tag.
        </video>
        <div className="absolute top-0 left-0 w-full h-full bg-gradient-to-b from-black/70 via-black/40 to-black/70"></div>
      </div>

      {/* Add top padding (pt-28) to account for the fixed navbar's height and position */}
      <div className="min-h-screen p-4 pt-28 flex flex-col items-center justify-center relative z-10">
        {/* Header */}
        <header className="w-full max-w-6xl flex justify-between items-center mb-8 mt-4">
          <div className="text-left">
            <h1 className="text-2xl lg:text-3xl font-bold bg-gradient-to-r from-pink-300 to-purple-300 bg-clip-text text-transparent">
              Welcome back, {user?.Name}!
            </h1>
            <p className="text-sm text-white/70 mt-1">
              Ready to spread some love today?
            </p>
          </div>
          <Button 
            onClick={handleLogout} 
            variant="ghost" 
            className="bg-white/5 hover:bg-white/10 text-white rounded-full text-sm px-4 py-2 border border-white/10"
          >
            <LogOut className="w-4 h-4 mr-2" />
            Logout
          </Button>
        </header>

        {/* Main Content */}
        <main className="w-full max-w-6xl grid grid-cols-1 lg:grid-cols-12 gap-6 mb-8">
          
          {/* Left Column: User Info & Trending */}
          <div className="lg:col-span-4 flex flex-col gap-6">
            {/* User Info Card */}
            <GlassCard className="p-6 flex flex-col">
              <div className="flex items-center gap-4 mb-5">
                <div className="w-14 h-14 bg-gradient-to-br from-purple-500 to-pink-500 rounded-full flex items-center justify-center overflow-hidden border-2 border-white/30 shadow-lg">
                  {profilePictureUrl ? (
                    <img
                      src={profilePictureUrl}
                      alt="Profile"
                      className="w-full h-full object-cover"
                      onError={(e) => {
                        // Fallback to initial if image fails to load
                        (e.target as HTMLImageElement).style.display = 'none';
                      }}
                    />
                  ) : null}
                  {!profilePictureUrl && (
                    <span className="text-xl font-bold text-white">
                      {user?.Name?.charAt(0)}
                    </span>
                  )}
                </div>
                <div>
                  <p className="font-bold text-lg">{user?.Name}</p>
                  <p className="text-sm text-white/70 mt-1">Reg: {user?.Regno}</p>
                </div>
              </div>
              
              <Button 
                onClick={() => handleNavigation('profile')}
                className="w-full bg-white/5 hover:bg-white/10 text-white mt-2 justify-between"
                variant="outline"
              >
                <span>View Profile</span>
                <ChevronRight className="w-4 h-4" />
              </Button>
            </GlassCard>

            {/* Trending Confessions */}
            <GlassCard className="p-6 flex-grow flex flex-col">
              <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
                <Heart className="w-5 h-5 text-pink-400" />
                Trending Confessions
              </h3>
              {isLoading ? (
                <div className="space-y-4">
                  {[...Array(4)].map((_, index) => (
                    <div key={index} className="p-4 bg-gradient-to-r from-white/5 to-white/0 rounded-lg text-sm text-white/90 border-l-2 border-pink-500/50">
                      <div className="flex">
                        <div className="flex-shrink-0 mr-3 mt-1">
                          <div className="w-2 h-2 rounded-full bg-pink-500"></div>
                        </div>
                        <div className="h-4 bg-white/20 rounded w-full animate-pulse"></div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="space-y-4 overflow-y-auto pr-2 -mr-2">
                  {trendingConfessions.map((confession, index) => (
                    <div 
                      key={index} 
                      className="p-4 bg-gradient-to-r from-white/5 to-white/0 rounded-lg text-sm text-white/90 border-l-2 border-pink-500/50 hover:border-pink-500 transition-all duration-300"
                    >
                      <div className="flex">
                        <div className="flex-shrink-0 mr-3 mt-1">
                          <div className="w-2 h-2 rounded-full bg-pink-500"></div>
                        </div>
                        <p>"{confession}"</p>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </GlassCard>
          </div>

          {/* Center Column: Main Navigation */}
          <div className="lg:col-span-8">
            <div className="grid grid-cols-2 gap-5">
              {navigationItems.map((item) => {
                const IconComponent = item.icon;
                return (
                  <GlassCard 
                    key={item.id}
                    className="cursor-pointer p-6 flex flex-col justify-between h-44"
                    onClick={() => handleNavigation(item.id)}
                  >
                    <div className="flex justify-between items-start">
                      <div>
                        <h3 className="text-xl font-semibold mb-2">{item.title}</h3>
                        <p className="text-sm text-white/70">{item.description}</p>
                      </div>
                      <div className="bg-gradient-to-br from-pink-500/20 to-rose-500/20 p-2 rounded-lg">
                        <IconComponent className="w-5 h-5 text-white" />
                      </div>
                    </div>
                    <div className="flex justify-end">
                      <div className="w-8 h-8 rounded-full bg-white/10 flex items-center justify-center group-hover:bg-white/20 transition-colors">
                        <ChevronRight className="w-4 h-4" />
                      </div>
                    </div>
                  </GlassCard>
                );
              })}
            </div>
            
            {/* About Promo */}
            <GlassCard className="cursor-pointer p-6 mt-5 flex items-center justify-between" onClick={() => handleNavigation('about')}>
              <div className="flex items-center gap-4">
                <div className="bg-gradient-to-br from-purple-500/20 to-indigo-500/20 p-3 rounded-xl">
                  <Info className="w-6 h-6 text-white" />
                </div>
                <div>
                  <h3 className="text-lg font-semibold">About ConfessIt</h3>
                  <p className="text-sm text-white/70">Learn more about our mission and features</p>
                </div>
              </div>
              <ChevronRight className="w-5 h-5 text-white/50" />
            </GlassCard>
          </div>
        </main>

        {/* Footer Note */}
        <footer className="text-center text-xs text-white/50 mt-4 mb-6">
          Made with ❤️ for connections that matter
        </footer>
      </div>
    </div>
  );
};
