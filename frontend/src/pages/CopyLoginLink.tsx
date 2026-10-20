import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Button } from "@/components/ui/button";

type CopyStatus = "copying" | "copied" | "failed" | "invalid";

const VERIFY_PAGE_PATH = "/verify-login";

/**
 * Email clients strip JavaScript, so a "Copy link" button in the email cannot
 * write to the clipboard directly. The email opens this page instead. It tries
 * to copy the exact sign-in URL on load and always exposes a manual copy button
 * for browsers that require a direct user gesture.
 */
const copyToClipboard = async (text: string): Promise<boolean> => {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall through to the legacy path below.
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);

  try {
    textarea.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.body.removeChild(textarea);
  }
};

export const CopyLoginLink = () => {
  const [searchParams] = useSearchParams();
  const rawUrl = searchParams.get("url") ?? "";

  const loginUrl = useMemo(() => {
    const candidate = rawUrl.trim();
    if (!candidate) return null;

    try {
      const parsed = new URL(candidate);
      if (
        parsed.origin !== window.location.origin ||
        parsed.pathname !== VERIFY_PAGE_PATH ||
        !parsed.searchParams.has("token")
      ) {
        return null;
      }
      return candidate;
    } catch {
      return null;
    }
  }, [rawUrl]);

  const [status, setStatus] = useState<CopyStatus>(loginUrl ? "copying" : "invalid");

  useEffect(() => {
    if (!loginUrl) return;

    let cancelled = false;
    void copyToClipboard(loginUrl).then((copied) => {
      if (!cancelled) setStatus(copied ? "copied" : "failed");
    });

    return () => {
      cancelled = true;
    };
  }, [loginUrl]);

  const handleCopy = async () => {
    if (!loginUrl) return;
    setStatus("copying");
    setStatus((await copyToClipboard(loginUrl)) ? "copied" : "failed");
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-4 py-10 text-foreground">
      <section className="w-full max-w-lg rounded-xl border bg-card p-6 shadow-sm">
        <h1 className="text-2xl font-semibold">Copy your sign-in link</h1>

        {status === "invalid" ? (
          <>
            <p className="mt-3 text-sm text-muted-foreground">
              This copy link is invalid or incomplete. Return to the login page and
              request a new sign-in link.
            </p>
            <Button asChild className="mt-6">
              <a href="/login">Go to login</a>
            </Button>
          </>
        ) : (
          <>
            <p className="mt-3 text-sm text-muted-foreground">
              {status === "copied"
                ? "The link is on your clipboard. You can close this tab and paste it into your browser."
                : status === "copying"
                  ? "Copying your sign-in link..."
                  : "Your browser blocked automatic copying. Use the Copy link button below."}
            </p>

            <p className="mt-5 break-all rounded-lg border bg-muted p-3 text-sm">
              {loginUrl}
            </p>

            <div className="mt-6 flex flex-wrap gap-3">
              <Button type="button" onClick={handleCopy} disabled={status === "copying"}>
                {status === "copied" ? "Copy again" : "Copy link"}
              </Button>
              <Button asChild variant="outline">
                <a href={loginUrl ?? "#"}>Open Link</a>
              </Button>
            </div>

            <p className="mt-4 text-xs text-muted-foreground" aria-live="polite">
              {status === "copied"
                ? "Copied to clipboard."
                : status === "copying"
                  ? "Requesting clipboard access..."
                  : "If copying still fails, select the full link above and copy it manually."}
            </p>
          </>
        )}
      </section>
    </main>
  );
};
