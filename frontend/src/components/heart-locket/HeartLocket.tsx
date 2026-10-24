import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import './heart-locket.css';

export type LocketState = 'closed' | 'opening' | 'open' | 'closing';

export const LOCKET_TRANSITION_MS = 950;

interface HeartLocketProps {
  state: LocketState;
  loginContent: ReactNode;
  verificationContent: ReactNode;
  onOpeningFinished: () => void;
  onClosingFinished: () => void;
}

const usePrefersReducedMotion = () => {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setReduced(query.matches);
    update();
    query.addEventListener?.('change', update);
    return () => query.removeEventListener?.('change', update);
  }, []);

  return reduced;
};

export const HeartLocket = ({
  state,
  loginContent,
  verificationContent,
  onOpeningFinished,
  onClosingFinished,
}: HeartLocketProps) => {
  const lidRef = useRef<HTMLDivElement>(null);
  const loginRef = useRef<HTMLDivElement>(null);
  const verificationRef = useRef<HTMLDivElement>(null);
  const previousStateRef = useRef<LocketState>(state);
  const reducedMotion = usePrefersReducedMotion();

  const finishTransition = useCallback(() => {
    if (state === 'opening') onOpeningFinished();
    else if (state === 'closing') onClosingFinished();
  }, [state, onOpeningFinished, onClosingFinished]);

  useEffect(() => {
    if (state !== 'opening' && state !== 'closing') return;

    if (reducedMotion) {
      const frame = window.requestAnimationFrame(finishTransition);
      return () => window.cancelAnimationFrame(frame);
    }

    const fallback = window.setTimeout(finishTransition, LOCKET_TRANSITION_MS + 140);
    return () => window.clearTimeout(fallback);
  }, [state, reducedMotion, finishTransition]);

  useEffect(() => {
    const previous = previousStateRef.current;
    previousStateRef.current = state;

    if (previous === 'opening' && state === 'open') {
      verificationRef.current?.focus({ preventScroll: true });
    }
    if (previous === 'closing' && state === 'closed') {
      loginRef.current?.querySelector<HTMLInputElement>('input')?.focus({ preventScroll: true });
    }
  }, [state]);

  const handleTransitionEnd = (event: React.TransitionEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    if (event.propertyName !== 'transform') return;
    finishTransition();
  };

  const lidOpen = state === 'opening' || state === 'open';
  const verificationVisible = state === 'open';

  return (
    <div className="heart-locket-stage" data-locket-state={state}>
      <div className="locket-thread" aria-hidden="true">
        <span className="locket-thread__cord" />
        <span className="locket-thread__bail" />
      </div>

      <div className="heart-locket-scene">
        <div
          className="locket-base"
          aria-hidden={state === 'closed'}
          inert={!verificationVisible}
        >
          <img
            className="locket-art locket-art--base"
            src="/heart_locket/confessit-locket-base-interior.svg"
            alt=""
            aria-hidden="true"
            draggable={false}
          />
          {verificationContent && (
            <div className="locket-verification-slot" ref={verificationRef} tabIndex={-1}>
              {verificationContent}
            </div>
          )}
        </div>

        <div
          ref={lidRef}
          className="locket-lid"
          data-lid-open={lidOpen ? 'true' : 'false'}
          onTransitionEnd={handleTransitionEnd}
        >
          <div
            className="locket-lid__face locket-lid__front"
            aria-hidden={state !== 'closed'}
            inert={state !== 'closed'}
          >
            <img
              className="locket-art locket-art--front"
              src="/heart_locket/confessit-locket-lid-front.svg"
              alt=""
              aria-hidden="true"
              draggable={false}
            />
            {loginContent && (
              <div className="locket-login-slot" ref={loginRef}>
                {loginContent}
              </div>
            )}
          </div>

          <div className="locket-lid__face locket-lid__inside" aria-hidden={state === 'closed'}>
            <img
              className="locket-art locket-art--inside"
              src="/heart_locket/confessit-locket-lid-inside.svg"
              alt=""
              aria-hidden="true"
              draggable={false}
            />
          </div>
        </div>

        <span className="locket-hinge" aria-hidden="true" />
      </div>
    </div>
  );
};
