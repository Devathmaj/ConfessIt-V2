import { describe, it, expect, vi, afterEach } from 'vitest';

/**
 * The single API origin (`src/lib/apiBase.ts`).
 *
 * Every REST call and WebSocket is built from `API_URL`, which resolves from
 * `VITE_API_URL` first and falls back to local HTTP outside production. These tests
 * pin the resolution: the variable wins when set (trailing slash stripped), and the
 * socket scheme follows the API scheme, so one variable can never fix chat while
 * leaving auth on localhost again.
 *
 * The module reads the environment at import, so each case re-imports it after
 * stubbing. The production fallback (`import.meta.env.PROD`) is deliberately not
 * covered here: vitest runs with `PROD` off, and faking the flag would assert on the
 * harness rather than the code.
 */

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

const load = () => import('@/lib/apiBase');

describe('apiBase', () => {
  it('defaults to local HTTP when nothing is configured', async () => {
    const { API_URL } = await load();
    expect(API_URL).toBe('http://localhost:8001');
  });

  it('honours VITE_API_URL and strips a trailing slash', async () => {
    vi.stubEnv('VITE_API_URL', 'https://api.example.com/');
    const { API_URL } = await load();
    expect(API_URL).toBe('https://api.example.com');
  });

  it('derives a wss:// socket from an https:// API origin', async () => {
    vi.stubEnv('VITE_API_URL', 'https://api.example.com');
    const { apiWsBase } = await load();
    expect(apiWsBase()).toBe('wss://api.example.com');
  });

  it('derives a ws:// socket from a plain-http API origin', async () => {
    vi.stubEnv('VITE_API_URL', 'http://192.168.1.10:8001');
    const { API_URL, apiWsBase } = await load();
    expect(API_URL).toBe('http://192.168.1.10:8001');
    expect(apiWsBase()).toBe('ws://192.168.1.10:8001');
  });
});
