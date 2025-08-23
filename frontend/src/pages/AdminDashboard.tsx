import { useAuth } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { CountdownTimer } from '@/components/ui/countdown-timer';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { 
  BarChart3, 
  Users, 
  Heart, 
  AlertTriangle, 
  TrendingUp, 
  MessageSquare,
  Shield,
  LogOut,
  Eye,
  Ban
} from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';

const mockAnalytics = [
  { label: 'Total Users', value: '2,847', change: '+12%', icon: Users },
  { label: 'Active Sessions', value: '1,247', change: '+8%', icon: TrendingUp },
  { label: 'Confessions Today', value: '892', change: '+24%', icon: MessageSquare },
  { label: 'Matches Made', value: '156', change: '+15%', icon: Heart },
];

const flaggedContent = [
  { id: 1, type: 'confession', content: 'Inappropriate content example...', reporter: 'User123', time: '2 min ago' },
  { id: 2, type: 'love-note', content: 'Spam message detected...', reporter: 'User456', time: '5 min ago' },
  { id: 3, type: 'confession', content: 'Potentially harmful content...', reporter: 'User789', time: '8 min ago' },
];

const recentActivity = [
  'User joined ConfessIt - 2 min ago',
  'Confession reported by User123 - 3 min ago',
  'Match made between User456 and User789 - 5 min ago',
  'Love note sent - 7 min ago',
  'User updated profile - 10 min ago',
];

export const AdminDashboard = () => {
  const { user, logout } = useAuth();

  const handleRemoveContent = (contentId: number) => {
    toast.success(`Content ${contentId} has been removed successfully 🛡️`);
  };

  const handleViewContent = (contentId: number) => {
    toast.info(`Viewing detailed report for content ${contentId}`);
  };

  const handleLogout = () => {
    logout();
    toast.success('Admin session ended 👑');
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
              Admin Dashboard 👑
            </h1>
            <p className="text-xl text-muted-foreground">
              Welcome, {user?.Name} - Keep ConfessIt safe and lovely!
            </p>
          </div>
          
          <div className="flex items-center gap-4">
            <CountdownTimer />
            <Button onClick={handleLogout} variant="outline" size="sm">
              <LogOut className="w-4 h-4 mr-2" />
              Logout
            </Button>
          </div>
        </div>
      </div>

      <div className="max-w-7xl mx-auto space-y-8">
        {/* Analytics Overview */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
          {mockAnalytics.map((stat, index) => {
            const IconComponent = stat.icon;
            return (
              <Card key={index} className="confession-card">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                  <CardTitle className="text-sm font-medium text-muted-foreground">
                    {stat.label}
                  </CardTitle>
                  <IconComponent className="h-5 w-5 text-romantic" />
                </CardHeader>
                <CardContent>
                  <div className="text-2xl font-bold text-romantic">{stat.value}</div>
                  <p className="text-xs text-green-600 font-medium">
                    {stat.change} from yesterday
                  </p>
                </CardContent>
              </Card>
            );
          })}
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
          {/* Flagged Content */}
          <div className="lg:col-span-2">
            <Card className="confession-card">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-2xl font-dancing text-romantic">
                  <AlertTriangle className="w-6 h-6 text-orange-500" />
                  Flagged Content
                </CardTitle>
                <CardDescription>
                  Content that requires moderator attention
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="space-y-4">
                  {flaggedContent.map((item) => (
                    <div key={item.id} className="p-4 bg-gradient-love rounded-lg border border-orange-200">
                      <div className="flex justify-between items-start mb-2">
                        <span className="text-sm font-medium text-romantic-dark capitalize">
                          {item.type} #{item.id}
                        </span>
                        <span className="text-xs text-muted-foreground">{item.time}</span>
                      </div>
                      <p className="text-sm text-muted-foreground mb-3 truncate">
                        "{item.content}"
                      </p>
                      <div className="flex justify-between items-center">
                        <span className="text-xs text-muted-foreground">
                          Reported by: {item.reporter}
                        </span>
                        <div className="flex gap-2">
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => handleViewContent(item.id)}
                          >
                            <Eye className="w-3 h-3 mr-1" />
                            View
                          </Button>
                          <Button
                            size="sm"
                            variant="destructive"
                            onClick={() => handleRemoveContent(item.id)}
                          >
                            <Ban className="w-3 h-3 mr-1" />
                            Remove
                          </Button>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          </div>

          {/* Sidebar */}
          <div className="space-y-6">
            {/* Quick Actions */}
            <Card className="confession-card">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-xl font-dancing text-romantic">
                  <Shield className="w-5 h-5" />
                  Quick Actions
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <Button className="w-full btn-romantic" size="sm">
                  <BarChart3 className="w-4 h-4 mr-2" />
                  View Full Analytics
                </Button>
                <Button className="w-full btn-love" size="sm">
                  <Users className="w-4 h-4 mr-2" />
                  Manage Users
                </Button>
                <Button className="w-full btn-love" size="sm">
                  <MessageSquare className="w-4 h-4 mr-2" />
                  Content Reports
                </Button>
              </CardContent>
            </Card>

            {/* Recent Activity */}
            <Card className="confession-card">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-xl font-dancing text-romantic">
                  <TrendingUp className="w-5 h-5" />
                  Recent Activity
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="space-y-3">
                  {recentActivity.map((activity, index) => (
                    <div
                      key={index}
                      className="text-sm text-muted-foreground p-2 bg-gradient-love rounded animate-fade-in"
                      style={{ animationDelay: `${index * 0.1}s` }}
                    >
                      {activity}
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>

            {/* System Health */}
            <Card className="confession-card">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-xl font-dancing text-romantic">
                  <Heart className="w-5 h-5 animate-pulse-heart" fill="currentColor" />
                  System Health
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Server Status:</span>
                  <span className="font-semibold text-green-600">Healthy</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Response Time:</span>
                  <span className="font-semibold text-romantic">89ms</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Uptime:</span>
                  <span className="font-semibold text-romantic">99.9%</span>
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
};