import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Heart, Mail } from 'lucide-react';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { toast } from 'sonner';
import ConfessItWebP from '@/assets/ConfessIt.webp';
import ConfessItWebM from '@/assets/ConfessIt.webm';
import ConfessItMP4 from '@/assets/ConfessIt.mp4';

export const Login = () => {
  const [regno, setRegno] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);

    try {
      const response = await fetch('http://localhost:8001/auth/login/magic', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ regno }),
      });

      if (response.ok) {
        toast.success('Magic link sent! Check your backend console.');
      } else {
        toast.error('Failed to generate magic link. Please try again.');
      }
    } catch (error) {
      toast.error('An error occurred. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center p-4 relative overflow-hidden">
      <div className="absolute top-0 left-0 w-full h-full z-0">
        <video
          poster={ConfessItWebP}
          autoPlay
          loop
          muted
          playsInline
          className="w-full h-full object-cover"
        >
          <source src={ConfessItWebM} type="video/webm" />
          <source src={ConfessItMP4} type="video/mp4" />
          Your browser does not support the video tag.
        </video>
        <div className="absolute top-0 left-0 w-full h-full bg-black/50"></div>
      </div>
      
      <FloatingHearts />
      
      <div className="w-full max-w-md z-10">
        <Card className="confession-card border-2 border-romantic/20 bg-background/80 backdrop-blur-sm">
          <CardHeader className="text-center space-y-4">
            <div className="mx-auto w-20 h-20 bg-gradient-romantic rounded-full flex items-center justify-center mb-4">
              <Heart className="w-10 h-10 text-white animate-pulse-heart" fill="currentColor" />
            </div>
            <CardTitle className="text-4xl font-dancing text-romantic">
              ConfessIt
            </CardTitle>
            <CardDescription className="text-lg text-muted-foreground">
              Share your heart in our Valentine's Day celebration 💕
            </CardDescription>
          </CardHeader>

          <CardContent className="space-y-6">
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="space-y-2">
                <div className="relative">
                  <Mail className="absolute left-3 top-3 h-5 w-5 text-muted-foreground" />
                  <Input
                    type="text"
                    placeholder="Registration Number"
                    value={regno}
                    onChange={(e) => setRegno(e.target.value)}
                    className="pl-10 bg-background/80 border-romantic/30 focus:border-romantic"
                    required
                  />
                </div>
              </div>

              <Button 
                type="submit" 
                className="w-full btn-romantic text-lg font-semibold"
                disabled={isLoading}
              >
                {isLoading ? 'Generating Link...' : 'Enter ConfessIt 💕'}
              </Button>
            </form>

            <div className="text-center mt-6 text-sm text-muted-foreground">
              <p className="sparkles">Made with love for Valentine's Day 2026</p>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
};
