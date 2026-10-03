import { NavLink } from 'react-router-dom';
import { HeartHandshake } from 'lucide-react';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

/**
 * Plot Twist (barebones couple game).
 *
 * No gameplay yet: this screen only reserves the catalogue route while the
 * real implementation lands later. The game is listed in the library and
 * honours the same hidden/disabled switches as every other title.
 */
export const PlotTwistGamePage = () => {
  return (
    <div>
      <GamesPageHeader
        eyebrow="Couple game"
        title="Plot Twist"
        description="A game for two — gameplay is still being built."
      />
      <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-xl">
            <HeartHandshake className="h-5 w-5" />
            Coming soon
          </CardTitle>
          <CardDescription>
            Plot Twist lives in the game library, but there is nothing to play yet.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            The full rules, sessions and scoring land here later. Until then this page
            only holds the route so the library, featured spotlight and admin console
            can reference it.
          </p>
          <Button asChild variant="outline" size="sm" className="rounded-full">
            <NavLink to="/games/select">Back to game library</NavLink>
          </Button>
        </CardContent>
      </Card>
    </div>
  );
};

export default PlotTwistGamePage;
