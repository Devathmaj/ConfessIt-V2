import { useState, useEffect } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { 
  Users, 
  Heart, 
  MessageCircle,
  LogOut,
  ChevronRight,
  Mail,
  Activity,
  Eye
} from 'lucide-react';
import { toast } from 'sonner';
import { getTotalConfessionsCount } from '../services/api';
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
  value?: string | number;
  isLoading?: boolean;
}

// Mock data for recent activities
const recentActivity = [
  'User "JohnDoe" signed up.',
  'A new confession was posted anonymously.',
  'Love note from "JaneDoe" is pending review.',
  'Match made between two users.',
  'User "PeterPan" updated their profile.',
];

export const AdminDashboard = () => {
  const { user, logout } = useAuth();
  const [profilePictureUrl, setProfilePictureUrl] = useState<string | null>(null);
  const [totalConfessions, setTotalConfessions] = useState<number | null>(null);
  const [isLoadingConfessions, setIsLoadingConfessions] = useState(true);

  useEffect(() => {
    // Set profile picture URL if available
    setProfilePictureUrl(resolveProfilePictureUrl(user?.profile_picture_id ?? null));

    // Fetch total confessions count
    const fetchConfessionCount = async () => {
      try {
        setIsLoadingConfessions(true);
        const count = await getTotalConfessionsCount();
        setTotalConfessions(count);
      } catch (error) {
        console.error('Failed to fetch total confessions count:', error);
        toast.error('Could not load total confessions.');
      } finally {
        setIsLoadingConfessions(false);
      }
    };

    fetchConfessionCount();
  }, [user]);
  
  // Data for the main navigation cards
  const adminNavigationItems: NavigationItem[] = [
    {
      id: 'total-users',
      title: 'Total Users',
      description: 'Total registered users',
      icon: Users,
      value: '2,847', // Mock data
    },
    {
      id: 'active-sessions',
      title: 'Active Sessions',
      description: 'Users currently online',
      icon: Activity,
      value: '1,247', // Mock data
    },
    {
      id: 'total-confessions',
      title: 'Total Confessions',
      description: 'All confessions ever made',
      icon: MessageCircle,
      value: totalConfessions ?? '...',
      isLoading: isLoadingConfessions,
    },
    {
      id: 'matches-made',
      title: 'Matches Made',
      description: 'Successful matches',
      icon: Heart,
      value: '156', // Mock data
    }
  ];

  /**
   * Used to handle navigation to different application pages.
   * @param {string} pageId - The identifier for the page to navigate to.
   */
  const handleNavigation = (pageId: string) => {
    toast.info(`Navigating to ${pageId} section...`);
    // Add actual navigation logic here if needed
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
  
  const StatCard = ({ item }: { item: NavigationItem }) => {
    const IconComponent = item.icon;
    return (
      <GlassCard 
        key={item.id}
        className="p-6 flex flex-col justify-between"
      >
        <div className="flex justify-between items-start">
          <div>
            <p className="text-sm text-white/70">{item.title}</p>
            {item.isLoading ? (
              <div className="h-8 w-24 bg-white/20 rounded animate-pulse mt-1"></div>
            ) : (
              <p className="text-3xl font-bold mt-1">{item.value}</p>
            )}
            <p className="text-xs text-white/50 mt-2">{item.description}</p>
          </div>
          <div className="bg-gradient-to-br from-pink-500/20 to-rose-500/20 p-3 rounded-lg">
            <IconComponent className="w-6 h-6 text-white" />
          </div>
        </div>
      </GlassCard>
    );
  };

  return (
    <div className="min-h-screen w-full text-white font-sans overflow-hidden">
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

      <div className="min-h-screen p-4 pt-28 flex flex-col items-center justify-center relative z-10">
        {/* Header */}
        <header className="w-full max-w-6xl flex justify-between items-center mb-8 mt-4">
          <div className="text-left">
            <h1 className="text-2xl lg:text-3xl font-bold bg-gradient-to-r from-pink-300 to-purple-300 bg-clip-text text-transparent">
              Welcome admin, {user?.Name}!
            </h1>
            <p className="text-sm text-white/70 mt-1">
              Moderate and monitor the application.
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
          
          {/* Left Column: Admin Info & Recent Activity */}
          <div className="lg:col-span-4 flex flex-col gap-6">
            {/* Admin Info Card */}
            <GlassCard className="p-6 flex flex-col">
              <div className="flex items-center gap-4 mb-5">
                <div className="w-14 h-14 bg-gradient-to-br from-purple-500 to-pink-500 rounded-full flex items-center justify-center overflow-hidden border-2 border-white/30 shadow-lg">
                  {profilePictureUrl ? (
                    <img
                      src={profilePictureUrl}
                      alt="Profile"
                      className="w-full h-full object-cover"
                      onError={(e) => {
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

            {/* Recent Activity */}
            <GlassCard className="p-6 flex-grow flex flex-col">
              <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
                <Activity className="w-5 h-5 text-pink-400" />
                Recent Activity
              </h3>
              <div className="space-y-4 overflow-y-auto pr-2 -mr-2">
                {recentActivity.map((activity, index) => (
                  <div 
                    key={index} 
                    className="p-3 bg-gradient-to-r from-white/5 to-white/0 rounded-lg text-sm text-white/90 border-l-2 border-pink-500/50"
                  >
                    <div className="flex items-center">
                      <div className="flex-shrink-0 mr-3">
                        <div className="w-2 h-2 rounded-full bg-pink-500"></div>
                      </div>
                      <p>{activity}</p>
                    </div>
                  </div>
                ))}
              </div>
            </GlassCard>
          </div>

          {/* Center Column: Main Stats */}
          <div className="lg:col-span-8">
            <div className="grid grid-cols-2 gap-5">
              {adminNavigationItems.map((item) => (
                <StatCard key={item.id} item={item} />
              ))}
            </div>
            
            {/* Love Notes Pending Review */}
            <GlassCard className="cursor-pointer p-6 mt-5 flex items-center justify-between" onClick={() => handleNavigation('love-notes-review')}>
              <div className="flex items-center gap-4">
                <div className="bg-gradient-to-br from-purple-500/20 to-indigo-500/20 p-3 rounded-xl">
                  <Mail className="w-6 h-6 text-white" />
                </div>
                <div>
                  <h3 className="text-lg font-semibold">Love Notes Pending Review</h3>
                  <p className="text-sm text-white/70">Moderate user-submitted love notes</p>
                </div>
              </div>
              <ChevronRight className="w-5 h-5 text-white/50" />
            </GlassCard>
          </div>
        </main>

        {/* Footer Note */}
        <footer className="text-center text-xs text-white/50 mt-4 mb-6">
          Admin Control Panel | ConfessIt
        </footer>
      </div>
    </div>
  );
};
