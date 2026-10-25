import { useRef, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { LegalDocument } from '@/components/LegalDocument';
import { HeartDivider } from '@/components/edge/EdgeOrnament';
import { Heart, Info, Users, Gamepad2, Mail, Shuffle } from 'lucide-react';
import { Navigation } from '@/components/Navigation';
import { AdminNavigation } from '@/components/AdminNavigation';
import { useAuth } from '@/contexts/AuthContext';
import { isStaffAccount } from '@/lib/capabilities';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { FloralDecor } from '@/components/FloralDecor';
import { ScatteredIcons } from '@/components/ScatteredIcons';
import aboutDocument from '../../../docs/About.md?raw';
import privacyPolicy from '../../../docs/Privacy-Policy.md?raw';
import termsOfService from '../../../docs/Terms-of-Service.md?raw';
import disclaimer from '../../../docs/Disclaimer.md?raw';

export const About = () => {
  const [activeDocument, setActiveDocument] = useState('about');
  const policiesRef = useRef<HTMLDivElement>(null);
  const { user } = useAuth();
  const isStaff = isStaffAccount(user?.capabilities, user?.account_type);

  // Switching documents should start the reader at the top of the policy section rather
  // than leaving them halfway down a different policy.
  const changeDocument = (value: string) => {
    setActiveDocument(value);
    policiesRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <div className="min-h-screen p-4 pt-24 relative overflow-hidden">
      {isStaff ? <AdminNavigation /> : <Navigation />}
      <FloatingHearts />
      {/* Floral preview: cropped blossom, left edge centered. */}
      <FloralDecor pair={5} className="left-0 top-1/2 w-[32rem] -translate-y-1/2 opacity-90 sm:w-[38rem]" />
      {/* Floral edges: 1706715_top top-right; 1706715_bottom bottom-left;
          1706715_top mirrored on the horizontal axis bottom-right. */}
      <FloralDecor pair={6} className="right-0 top-0 w-[34rem] opacity-90 sm:w-[42rem]" />
      <FloralDecor pair={7} className="bottom-0 left-0 w-[34rem] opacity-90 sm:w-[42rem]" />
      <FloralDecor pair={6} className="bottom-0 right-0 w-[34rem] -scale-y-100 opacity-90 sm:w-[42rem]" />
      {/* Scattered keepsakes for the side gutters between sections. */}
      <ScatteredIcons variant="romance" />
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

        <div className="my-8" aria-hidden>
          <HeartDivider />
        </div>

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
                Games & Icebreakers
              </CardTitle>
            </CardHeader>
            <CardContent className="text-muted-foreground space-y-3">
              <p>Break the ice in the Games section: pick from a growing library of mini-games, chase daily featured picks and climb the leaderboards. Play with your matches and get to know them in a fun and relaxed way — Truth or Dare, Compatibility Quiz and more are waiting.</p>
            </CardContent>
          </Card>
        </div>

        <div ref={policiesRef} className="mt-12 min-w-0 scroll-mt-24">
          <h2 className="text-4xl font-dancing text-romantic text-center mb-2">Information & Policies</h2>
          <p className="text-center text-muted-foreground mb-6">
            Read about ConfessIt, our privacy practices, terms of service, and disclaimer.
          </p>
          <Tabs
            value={activeDocument}
            onValueChange={changeDocument}
            className="confession-card min-w-0 max-w-full p-4 sm:p-6"
          >
            <TabsList className="grid h-auto w-full grid-cols-2 gap-1 p-1 sm:grid-cols-4">
              <TabsTrigger value="about">About</TabsTrigger>
              <TabsTrigger value="privacy">Privacy Policy</TabsTrigger>
              <TabsTrigger value="terms">Terms of Service</TabsTrigger>
              <TabsTrigger value="disclaimer">Disclaimer</TabsTrigger>
            </TabsList>
            <TabsContent value="about" className="mt-6 min-w-0">
              <LegalDocument markdown={aboutDocument} onDocumentLink={changeDocument} />
            </TabsContent>
            <TabsContent value="privacy" className="mt-6 min-w-0">
              <LegalDocument markdown={privacyPolicy} onDocumentLink={changeDocument} />
            </TabsContent>
            <TabsContent value="terms" className="mt-6 min-w-0">
              <LegalDocument markdown={termsOfService} onDocumentLink={changeDocument} />
            </TabsContent>
            <TabsContent value="disclaimer" className="mt-6 min-w-0">
              <LegalDocument markdown={disclaimer} onDocumentLink={changeDocument} />
            </TabsContent>
          </Tabs>
        </div>

        <div className="mt-8" aria-hidden>
          <HeartDivider />
        </div>
      </div>
    </div>
  );
};
