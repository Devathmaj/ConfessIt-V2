// Dev-only logging. Production builds substitute every console method with a
// no-op (see vite.config.ts), so calls routed through here disappear from the
// shipped bundle while development keeps the diagnostics.
/* eslint-disable no-console */
type LogMethod = (...args: unknown[]) => void;

const noop: LogMethod = () => {};

const enabled = import.meta.env.DEV;

export const logger = {
  log: enabled ? (...args: unknown[]) => console.log(...args) : noop,
  info: enabled ? (...args: unknown[]) => console.info(...args) : noop,
  warn: enabled ? (...args: unknown[]) => console.warn(...args) : noop,
  error: enabled ? (...args: unknown[]) => console.error(...args) : noop,
  debug: enabled ? (...args: unknown[]) => console.debug(...args) : noop,
  trace: enabled ? (...args: unknown[]) => console.trace(...args) : noop,
};
