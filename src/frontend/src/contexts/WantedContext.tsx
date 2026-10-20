import type { ReactNode } from 'react';
import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react';

import { useDependencyEffect } from '../hooks/useMountEffect';
import {
  addWanted,
  bookmarkAllForWanted,
  clearObtainedWanted,
  clearWantedBlocklist,
  evaluateWantedReleases,
  getWantedAttempts,
  grabWantedRelease,
  listWantedWatches,
  getWantedStatus,
  listWanted,
  pauseWanted,
  removeWanted,
  resumeWanted,
  searchWantedNow,
  requestToWanted,
  setWantedLanguages,
  unblockWantedRelease,
  unwatchForWanted,
  watchForWanted,
} from '../services/api';
import type {
  Book,
  ContentType,
  Language,
  Release,
  WantedAttempt,
  WantedBookState,
  WantedItem,
  WantedWatch,
  WantedWatchTarget,
} from '../types';
import {
  buildWantedBookPayload,
  indexOwnWantedItems,
  isBookmarkable,
  resolveWantedState,
  wantedKey,
} from '../utils/wanted';
import { useSocket } from './SocketContext';

type ShowToast = (message: string, type?: 'info' | 'success' | 'error') => void;

interface WantedContextValue {
  enabled: boolean;
  canBookmark: boolean;
  isAdmin: boolean;
  items: WantedItem[];
  watches: WantedWatch[];
  loading: boolean;
  canBookmarkBook: (book: Book) => boolean;
  bookLanguages: Language[];
  defaultLanguageCodes: string[];
  stateFor: (book: Book) => WantedBookState | undefined;
  toggle: (book: Book, languages?: string[] | null) => Promise<void>;
  remove: (item: WantedItem, scope?: 'all') => Promise<void>;
  searchNow: (item: WantedItem) => Promise<void>;
  setPaused: (item: WantedItem, paused: boolean) => Promise<void>;
  clearBlocklist: (item: WantedItem) => Promise<void>;
  setLanguages: (item: WantedItem, languages: string[] | null) => Promise<void>;
  clearObtained: () => Promise<void>;
  loadAttempts: (item: WantedItem) => Promise<WantedAttempt[]>;
  unblockRelease: (item: WantedItem, attempt: WantedAttempt) => Promise<void>;
  /** Why the wanted list would skip each release ("source:id" -> reason or null). */
  evaluateReleases: (
    wantedId: number,
    releases: Release[],
  ) => Promise<Record<string, string | null>>;
  /** Download a picked release for the wanted list; false if it could not be queued. */
  grab: (wantedId: number, release: Release, title: string) => Promise<boolean>;
  /** Bookmark every book of a series, author or list not in the library yet. */
  bookmarkAll: (target: WantedWatchTarget, userId?: number | null) => Promise<void>;
  /** Bookmark new books of a series, author or list as they appear. */
  watch: (target: WantedWatchTarget, userId?: number | null) => Promise<void>;
  unwatch: (watch: WantedWatch) => Promise<void>;
  approveRequestToWanted: (requestId: number, title: string) => Promise<void>;
}

const WantedContext = createContext<WantedContextValue | null>(null);

const DISABLED_VALUE: WantedContextValue = {
  enabled: false,
  canBookmark: false,
  isAdmin: false,
  items: [],
  watches: [],
  loading: false,
  canBookmarkBook: () => false,
  bookLanguages: [],
  defaultLanguageCodes: [],
  stateFor: () => undefined,
  toggle: async () => undefined,
  remove: async () => undefined,
  searchNow: async () => undefined,
  setPaused: async () => undefined,
  clearBlocklist: async () => undefined,
  setLanguages: async () => undefined,
  clearObtained: async () => undefined,
  loadAttempts: async () => [],
  unblockRelease: async () => undefined,
  evaluateReleases: async () => ({}),
  grab: async () => false,
  bookmarkAll: async () => undefined,
  watch: async () => undefined,
  unwatch: async () => undefined,
  approveRequestToWanted: async () => undefined,
};

/** Wanted list state; outside a provider every feature reads as switched off. */
export function useWanted(): WantedContextValue {
  return useContext(WantedContext) ?? DISABLED_VALUE;
}

interface WantedProviderProps {
  active: boolean;
  /** Bumped whenever settings are saved, so the wanted list re-reads its switches. */
  settingsVersion: number;
  isAdmin: boolean;
  contentType: ContentType;
  bookLanguages: Language[];
  defaultLanguageCodes: string[];
  onShowToast: ShowToast;
  children: ReactNode;
}

export function WantedProvider({
  active,
  settingsVersion,
  isAdmin,
  contentType,
  bookLanguages,
  defaultLanguageCodes,
  onShowToast,
  children,
}: WantedProviderProps) {
  const { socket } = useSocket();
  const [enabled, setEnabled] = useState(false);
  const [canBookmark, setCanBookmark] = useState(false);
  const [items, setItems] = useState<WantedItem[]>([]);
  const [watches, setWatches] = useState<WantedWatch[]>([]);
  const [listLoaded, setListLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const latestRequest = useRef(0);
  const [overrides, setOverrides] = useState<Map<string, WantedBookState | null>>(() => new Map());
  // Book key -> the item it joined, which may be keyed by another provider's book.
  const [linkedIds, setLinkedIds] = useState<Map<string, number>>(() => new Map());

  const refresh = useCallback(async () => {
    if (!active) {
      return;
    }
    latestRequest.current += 1;
    const request = latestRequest.current;
    setLoading(true);
    try {
      const status = await getWantedStatus();
      const [nextItems, nextWatches] = status.enabled
        ? await Promise.all([listWanted(), listWantedWatches()])
        : [[], []];
      // A newer refresh is in flight.
      if (request !== latestRequest.current) {
        return;
      }
      setEnabled(status.enabled);
      setCanBookmark(status.can_bookmark);
      setItems(nextItems);
      setWatches(nextWatches);
      setListLoaded(true);
      setOverrides(new Map());
    } catch (error) {
      console.warn('Failed to load the wanted list:', error);
    } finally {
      if (request === latestRequest.current) {
        setLoading(false);
      }
    }
  }, [active]);

  useDependencyEffect(() => {
    void refresh();
  }, [refresh, settingsVersion]);

  useDependencyEffect(() => {
    if (!socket || !active) {
      return undefined;
    }
    const onUpdate = () => {
      void refresh();
    };
    socket.on('wanted_update', onUpdate);
    return () => {
      socket.off('wanted_update', onUpdate);
    };
  }, [socket, active, refresh]);

  const ownItems = useMemo(() => indexOwnWantedItems(items), [items]);

  const stateFor = useCallback(
    (book: Book) => resolveWantedState(book, overrides, ownItems, linkedIds, listLoaded),
    [overrides, ownItems, linkedIds, listLoaded],
  );

  const canBookmarkBook = useCallback(
    (book: Book) => enabled && canBookmark && isBookmarkable(book, contentType),
    [enabled, canBookmark, contentType],
  );

  const run = useCallback(
    async (action: () => Promise<void>, success?: string) => {
      try {
        await action();
        if (success) {
          onShowToast(success, 'success');
        }
      } catch (error) {
        onShowToast(error instanceof Error ? error.message : 'Wanted list update failed', 'error');
      }
      await refresh();
    },
    [onShowToast, refresh],
  );

  const toggle = useCallback(
    async (book: Book, languages?: string[] | null) => {
      const payload = buildWantedBookPayload(book);
      if (!payload) {
        return;
      }
      const key = wantedKey(payload.provider, payload.provider_id);
      const current = stateFor(book);
      if (current?.subscribed) {
        await run(async () => {
          await removeWanted(current.id);
          setOverrides((prev) => new Map(prev).set(key, null));
        }, `Removed "${book.title}" from your wanted list`);
        return;
      }
      await run(async () => {
        const { item } = await addWanted(payload, languages);
        setLinkedIds((prev) => new Map(prev).set(key, item.id));
        setOverrides((prev) =>
          new Map(prev).set(key, {
            id: item.id,
            state: item.state,
            subscribed: true,
          }),
        );
      }, `"${book.title}" is on your wanted list and will download when available`);
    },
    [run, stateFor],
  );

  const value = useMemo<WantedContextValue>(
    () => ({
      enabled,
      canBookmark,
      isAdmin,
      items,
      watches,
      loading,
      canBookmarkBook,
      bookLanguages,
      defaultLanguageCodes,
      stateFor,
      toggle,
      remove: (item, scope) =>
        run(() => removeWanted(item.id, scope), `Removed "${item.title}" from the wanted list`),
      searchNow: (item) => run(() => searchWantedNow(item.id), `Searching for "${item.title}"`),
      setPaused: (item, paused) =>
        run(() => (paused ? pauseWanted(item.id) : resumeWanted(item.id))),
      clearBlocklist: (item) =>
        run(
          () => clearWantedBlocklist(item.id),
          `Failed and cancelled releases of "${item.title}" may be tried again`,
        ),
      setLanguages: (item, languages) =>
        run(() => setWantedLanguages(item.id, languages), `Languages saved for "${item.title}"`),
      clearObtained: () => run(clearObtainedWanted, 'Removed obtained books'),
      loadAttempts: (item) => getWantedAttempts(item.id),
      unblockRelease: (item, attempt) =>
        run(
          () => unblockWantedRelease(item.id, attempt.id),
          `"${attempt.release_title ?? 'Release'}" may be tried again`,
        ),
      evaluateReleases: evaluateWantedReleases,
      grab: async (wantedId, release, title) => {
        let queued = false;
        await run(async () => {
          await grabWantedRelease(wantedId, release);
          queued = true;
        }, `Downloading "${title}" for the wanted list`);
        return queued;
      },
      bookmarkAll: (target, userId) =>
        run(async () => {
          const result = await bookmarkAllForWanted(target, userId);
          const message = [
            `Bookmarked ${result.bookmarked} book(s) of ${target.label}`,
            result.already > 0 ? `${result.already} already bookmarked` : null,
            result.in_library > 0 ? `${result.in_library} already in the library` : null,
          ]
            .filter(Boolean)
            .join('; ');
          onShowToast(message, 'success');
        }),
      watch: (target, userId) =>
        run(
          () => watchForWanted(target, userId),
          `Watching ${target.label}: new books will be bookmarked`,
        ),
      unwatch: (entry) =>
        run(() => unwatchForWanted(entry.id), `No longer watching ${entry.label}`),
      approveRequestToWanted: (requestId, title) =>
        run(() => requestToWanted(requestId), `"${title}" is on the wanted list for the requester`),
    }),
    [
      enabled,
      canBookmark,
      isAdmin,
      items,
      watches,
      loading,
      canBookmarkBook,
      bookLanguages,
      defaultLanguageCodes,
      stateFor,
      toggle,
      run,
      onShowToast,
    ],
  );

  return <WantedContext.Provider value={value}>{children}</WantedContext.Provider>;
}
