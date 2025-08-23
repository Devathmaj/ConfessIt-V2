import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Heart, Info, Users, Gamepad2, Mail, Shuffle } from 'lucide-react';
import { Navigation } from '@/components/Navigation';

export const About = () => {
  return (
    <div className="min-h-screen p-4 pt-24 relative overflow-hidden">
      <Navigation />
      <div className="max-w-4xl mx-auto">
        <div className="text-center mb-12">
          <h1 className="text-6xl font-dancing text-romantic mb-4">About ConfessIt</h1>
          <p className="text-xl text-muted-foreground">Your safe space to connect and share.</p>
        </div>

        <Card className="mb-8 confession-card">
          <CardHeader>
            <CardTitle className="flex items-center gap-3 text-3xl font-dancing text-romantic">
              <Heart className="w-8 h-8" />
              Our Mission
            </CardTitle>
          </CardHeader>
          <CardContent className="text-lg text-muted-foreground space-y-4">
            <p>
              ConfessIt was born from a simple idea: to create a platform where individuals can share their deepest thoughts, feelings, and confessions without fear of judgment. We believe in the power of anonymity to foster genuine connection and self-expression.
            </p>
            <p>
              Our mission is to provide a supportive and loving community where you can be your true self, find others who understand you, and maybe even find a special connection along the way.
            </p>
          </CardContent>
        </Card>

        <Card className="mb-8 confession-card">
          <CardHeader>
            <CardTitle className="flex items-center gap-3 text-3xl font-dancing text-romantic">
              <Users className="w-8 h-8" />
              Who We Are
            </CardTitle>
          </CardHeader>
          <CardContent className="text-lg text-muted-foreground">
            <p>
              We are a passionate team of developers, designers, and dreamers who wanted to build something meaningful. This platform is a labor of love, and we are dedicated to making it a safe and enjoyable experience for everyone.
            </p>
          </CardContent>
        </Card>

        <div className="space-y-8">
          <h2 className="text-4xl font-dancing text-romantic text-center mb-8">Features Guide</h2>

          <Card className="confession-card">
            <CardHeader>
              <CardTitle className="flex items-center gap-3 text-2xl font-dancing text-romantic">
                <Info className="w-7 h-7" />
                Anonymous Confessions
              </CardTitle>
            </CardHeader>
            <CardContent className="text-muted-foreground space-y-3">
              <p>Share what's on your mind without revealing your identity. Your secrets are safe with us. You can react to confessions with hearts, laughs, and more. You can also comment on confessions to share your thoughts and support.</p>
            </CardContent>
          </Card>

          <Card className="confession-card">
            <CardHeader>
              <CardTitle className="flex items-center gap-3 text-2xl font-dancing text-romantic">
                <Mail className="w-7 h-7" />
                Love Notes
              </CardTitle>
            </CardHeader>
            <CardContent className="text-muted-foreground space-y-3">
              <p>Send beautiful, personalized digital love notes to your friends or secret crush. Choose from various templates and add your own special touch with emojis and stickers.</p>
            </CardContent>
          </Card>

          <Card className="confession-card">
            <CardHeader>
              <CardTitle className="flex items-center gap-3 text-2xl font-dancing text-romantic">
                <Shuffle className="w-7 h-7" />
                Matchmaking
              </CardTitle>
            </CardHeader>
            <CardContent className="text-muted-foreground space-y-3">
              <p>Feeling lucky? Try our random matchmaking feature to connect with someone new. You might just find your perfect match! You can start a conversation and get to know each other better.</p>
            </CardContent>
          </Card>

          <Card className="confession-card">
            <CardHeader>
              <CardTitle className="flex items-center gap-3 text-2xl font-dancing text-romantic">
                <Gamepad2 className="w-7 h-7" />
                Mini-Games & Icebreakers
              </CardTitle>
            </CardHeader>
            <CardContent className="text-muted-foreground space-y-3">
              <p>Break the ice with our fun and interactive mini-games. Play with your matches and get to know them in a fun and relaxed way. We have a variety of games to choose from, including Truth or Dare and Love Compatibility Quiz.</p>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
};
