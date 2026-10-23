import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { runInNewContext } from 'node:vm';

import { beforeEach, describe, expect, it, vi } from 'vitest';

type WorkerListener = (event: any) => void;

const cacheKey = (request: Request | string) =>
  typeof request === 'string' ? request : request.url;

const loadWorker = () => {
  const listeners = new Map<string, WorkerListener[]>();
  const stores = new Map<string, Map<string, Response>>();
  const cacheObjects = new Map<string, any>();

  const caches = {
    open: vi.fn(async (name: string) => {
      if (!cacheObjects.has(name)) {
        const store = new Map<string, Response>();
        stores.set(name, store);
        cacheObjects.set(name, {
          match: vi.fn(async (request: Request | string) => store.get(cacheKey(request))?.clone()),
          put: vi.fn(async (request: Request | string, response: Response) => {
            store.set(cacheKey(request), response.clone());
          }),
          delete: vi.fn(async (request: Request | string) => store.delete(cacheKey(request))),
        });
      }
      return cacheObjects.get(name);
    }),
    keys: vi.fn(async () => [...stores.keys()]),
    delete: vi.fn(async (name: string) => stores.delete(name)),
  };

  const fetchMock = vi.fn();
  const self = {
    location: { origin: 'https://app.test' },
    addEventListener: vi.fn((type: string, listener: WorkerListener) => {
      const entries = listeners.get(type) ?? [];
      entries.push(listener);
      listeners.set(type, entries);
    }),
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn() },
  };

  runInNewContext(readFileSync(resolve(process.cwd(), 'public/sw.js'), 'utf8'), {
    self,
    caches,
    fetch: fetchMock,
    Request,
    Response,
    URL,
    console,
  });

  const fetchHandler = listeners.get('fetch')?.[0];
  if (!fetchHandler) throw new Error('service worker did not register a fetch handler');

  return { fetchHandler, stores, fetchMock, caches };
};

const requestFor = (path: string) => new Request(`https://app.test${path}`);

const dispatch = (fetchHandler: WorkerListener, request: Request) => {
  const event = {
    request,
    respondWith: vi.fn(),
    waitUntil: vi.fn(),
  };
  fetchHandler(event);
  return event;
};

describe('service worker static cache policy', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('revalidates mutable JSON before serving the cached copy', async () => {
    const { fetchHandler, stores, fetchMock, caches } = loadWorker();
    await caches.open('confessit-static-v3');
    const request = requestFor('/templates/template_1.json');
    stores.get('confessit-static-v3')?.set(request.url, new Response('old'));
    fetchMock.mockResolvedValue(new Response('new', { status: 200 }));

    const event = dispatch(fetchHandler, request);
    const response = await event.respondWith.mock.calls[0][0];

    expect(fetchMock).toHaveBeenCalledWith(request, { cache: 'no-cache' });
    expect(await response.text()).toBe('new');
    expect(await stores.get('confessit-static-v3')?.get(request.url)?.text()).toBe('new');
  });

  it('falls back to the cached JSON only when the network fails', async () => {
    const { fetchHandler, stores, fetchMock, caches } = loadWorker();
    await caches.open('confessit-static-v3');
    const request = requestFor('/templates/template_2.json');
    stores.get('confessit-static-v3')?.set(request.url, new Response('offline-copy'));
    fetchMock.mockRejectedValue(new Error('offline'));

    const event = dispatch(fetchHandler, request);
    const response = await event.respondWith.mock.calls[0][0];

    expect(await response.text()).toBe('offline-copy');
  });

  it('keeps cache-first delivery for hashed immutable assets', async () => {
    const { fetchHandler, stores, fetchMock, caches } = loadWorker();
    await caches.open('confessit-static-v3');
    const request = requestFor('/assets/index-abc12345.js');
    stores.get('confessit-static-v3')?.set(request.url, new Response('cached-bundle'));

    const event = dispatch(fetchHandler, request);
    const response = await event.respondWith.mock.calls[0][0];

    expect(fetchMock).not.toHaveBeenCalled();
    expect(await response.text()).toBe('cached-bundle');
  });

  it('still refuses to handle sensitive API, auth and chat reads', () => {
    for (const path of [
      '/api/confessions?limit=1',
      '/api/auth/me',
      '/api/messages?conversation_id=1',
      '/api/admin/reports',
    ]) {
      const { fetchHandler } = loadWorker();
      const event = dispatch(fetchHandler, requestFor(path));
      expect(event.respondWith, path).not.toHaveBeenCalled();
    }
  });
});
