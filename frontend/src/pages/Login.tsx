import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Heart, Mail, ShieldCheck } from 'lucide-react';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { toast } from 'sonner';
import ConfessItWebP from '@/assets/ConfessIt.webp';
import ConfessItWebM from '@/assets/ConfessIt.webm';
import ConfessItMP4 from '@/assets/ConfessIt.mp4';
import { magicLinkEndpoint, SIGN_IN_CATEGORIES, type SignInCategory } from '@/lib/signInCategories';

/**
 * One sign-in form, rendered three times.
 *
 * Which account table a person is signing in to is decided by the door they came through, not
 * by anything they type. A regno is unique only within a table, so the same registration
 * number can be a participant and a moderator at once; posting to this page's own endpoint is
 * what makes the server read the right one, and it hands back a link for exactly that account
 * or no link at all. It never searches the other two tables and never falls back to one.
 *
 * The page is a convenience, not the enforcement: the category lives in the URL the browser
 * called, so editing this component cannot make one door serve another's accounts.
 */
const SignInPage = ({ category }: { category: SignInCategory }) => {
  const [regno, setRegno] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  /**
   * Turn the server's wait (seconds, from the `Retry-After` header on a 429) into the
   * sentence a person can act on. The rate-limited detail already says "try again
   * shortly"; the important part is how shortly, and the server is the only authority on
   * that.
   */
  const retryWaitSentence = (retryAfterSeconds: number): string => {
    if (retryAfterSeconds < 60) return 'You can try again in less than a minute.';
    const minutes = Math.ceil(retryAfterSeconds / 60);
    return `You can try again in about ${minutes} minute${minutes === 1 ? '' : 's'}.`;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);

    try {
      const response = await fetch(magicLinkEndpoint(category.accountType), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        // The API answers these requests with an opaque client-id cookie used only to
        // rate-limit login attempts per browser. Cross-origin fetches drop cookies
        // unless they opt in, so without this the server would mint a fresh identity
        // on every request and the per-browser limit would never engage.
        credentials: 'include',
        body: JSON.stringify({ regno }),
      });

      if (response.ok) {
        toast.success('Magic link sent! Check your backend console.');
      } else {
        const body = await response.json().catch(() => null);
        const detail = typeof body?.detail === 'string' ? body.detail : null;
        // A 429 names the wait it keeps in `Retry-After`, so the toast can say how long
        // instead of leaving "try again shortly" as the last word.
        const retryAfter = Number(response.headers.get('Retry-After'));
        const retryNote =
          Number.isFinite(retryAfter) && retryAfter > 0 ? retryWaitSentence(retryAfter) : null;
        if (response.status === 403) {
          toast.error(detail ?? 'Your account is currently not available.');
        } else if (retryNote) {
          toast.error(detail ? `${detail} ${retryNote}` : retryNote);
        } else {
          toast.error(detail ?? 'Failed to generate magic link. Please try again.');
        }
      }
    } catch (error) {
      toast.error('An error occurred. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  const form = (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="space-y-2">
        <div className="relative">
          <Mail className="absolute left-3 top-3 h-5 w-5 text-muted-foreground" />
          <Input
            type="text"
            placeholder={category.regnoPlaceholder}
            value={regno}
            onChange={(e) => setRegno(e.target.value)}
            className={
              category.themed
                ? 'pl-10 bg-background/80 border-romantic/30 focus:border-romantic'
                : 'pl-10'
            }
            required
          />
        </div>
      </div>

      <Button
        type="submit"
        className={category.themed ? 'w-full btn-romantic text-lg font-semibold' : 'w-full'}
        disabled={isLoading}
      >
        {isLoading ? category.pendingCta : category.cta}
      </Button>
    </form>
  );

  // The participant door keeps the full Valentine's treatment. Staff doors are plain on
  // purpose: a moderator reaching for the moderation console should not be looking at the
  // same screen a student sees, and there is no reason for a staff door to be pretty.
  if (!category.themed) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4 bg-muted/30">
        <div className="w-full max-w-md z-10">
          <Card>
            <CardHeader className="text-center space-y-3">
              <div className="mx-auto w-16 h-16 bg-muted rounded-full flex items-center justify-center">
                <ShieldCheck className="w-8 h-8 text-muted-foreground" />
              </div>
              <CardTitle className="text-2xl">{category.title}</CardTitle>
              <CardDescription>{category.tagline}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">{form}</CardContent>
          </Card>
        </div>
      </div>
    );
  }

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
              {category.title}
            </CardTitle>
            <CardDescription className="text-lg text-muted-foreground">
              {category.tagline}
            </CardDescription>
          </CardHeader>

          <CardContent className="space-y-6">
            {form}

            <div className="text-center mt-6 text-sm text-muted-foreground">
              <p className="sparkles">Made with love for Valentine's Day 2026</p>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
};

/** Participant door. A regno here is looked up in the `users` table and nowhere else. */
export const Login = () => <SignInPage category={SIGN_IN_CATEGORIES.users} />;

/** Admin door. Looks the regno up in the `admin` table and nowhere else. */
export const AdminLogin = () => <SignInPage category={SIGN_IN_CATEGORIES.admin} />;

/** Moderator door. Looks the regno up in the `moderator` table and nowhere else. */
export const ModeratorLogin = () => <SignInPage category={SIGN_IN_CATEGORIES.moderator} />;
