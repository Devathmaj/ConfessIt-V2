import { AdminNavigation } from '@/components/AdminNavigation';
import { Card, CardContent } from '@/components/ui/card';

/**
 * Operator-facing application settings.
 *
 * Intentionally empty for now. Platform settings are still environment variables read by
 * `app/config.py` at import time, so there is nothing to show or change until they become
 * stored settings. The screen exists behind `app_config:read`, which only an admin holds, so
 * this is where those settings will be rendered and edited once they exist.
 */
export const AppConfiguration = () => {
  return (
    <div className="relative min-h-screen bg-background">
      <AdminNavigation />

      <div className="mx-auto flex w-full max-w-6xl flex-col gap-8 px-4 pb-16 pt-28">
        <header className="flex flex-col gap-2">
          <h1 className="text-3xl font-semibold">App Configuration</h1>
          <p className="text-sm text-muted-foreground">
            Platform-wide settings. Visible to admins only.
          </p>
        </header>

        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            No settings are available yet.
          </CardContent>
        </Card>
      </div>
    </div>
  );
};