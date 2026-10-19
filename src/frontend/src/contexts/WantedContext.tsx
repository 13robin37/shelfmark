import type { ReactNode } from 'react';
import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react';

import { useDependencyEffect } from '../hooks/useMountEffect';
import {
  addWanted,
  clearObtainedWanted,
  clearWantedBlocklist,
  getWantedAttempts,
  getWantedStatus,
  listWanted,
  pauseWanted,
  removeWanted,
  resumeWanted,
  searchWantedNow,
  setWantedLanguages,
  unblockWantedRelease,
} from '../services/api';
import type {
  Book,
  ContentType,
  Language,
  WantedAttempt,
  WantedBookState,
  WantedItem,
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
  loading: boolean;
  canBookmarkBook: (book: Book) => boolean;
  bookLanguages: Language[];
  defaultLanguageCodes: string[];
  stateFor: (book: Book) => WantedBookState | undefined;
  /** ``languages`` overrides the viewer's default languages for this book. */
  toggle: (book: Book, languages?: string[] | null) => Promise<void>;
  remove: (item: WantedItem, scope?: 'all') => Promise<void>;
  searchNow: (item: WantedItem) => Promise<void>;
  setPaused: (item: WantedItem, paused: boolean) => Promise<void>;
  clearBlocklist: (item: WantedItem) => Promise<void>;
  setLanguages: (item: WantedItem, languages: string[] | null) => Promise<void>;
  clearObtained: () => Promise<void>;
  loadAttempts: (item: WantedItem) => Promise<WantedAttempt[]>;
  unblockRelease: (item: WantedItem, attempt: WantedAttempt) => Promise<void>;
  refresh: () => Promise<void>;
}

const WantedContext = createContext<WantedContextValue | null>(null);

const DISABLED_VALUE: WantedContextValue = {
  enabled: false,
  canBookmark: false,
  isAdmin: false,
  items: [],
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
  refresh: async () => undefined,
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
      const nextItems = status.enabled ? await listWanted() : [];
      // A later refresh has started; its answer is the newer one.
      if (request !== latestRequest.current) {
        return;
      }
      setEnabled(status.enabled);
      setCanBookmark(status.can_bookmark);
      setItems(nextItems);
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

  // The wanted list lives on the server; load it whenever the viewer can see it.
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
      if (!book.provider || !book.provider_id) {
        return;
      }
      const key = wantedKey(book.provider, book.provider_id);
      const current = stateFor(book);
      if (current?.subscribed) {
        await run(async () => {
          await removeWanted(current.id);
          setOverrides((prev) => new Map(prev).set(key, null));
        }, `Removed "${book.title}" from your wanted list`);
        return;
      }
      const payload = buildWantedBookPayload(book);
      if (!payload) {
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
        run(
          () => setWantedLanguages(item.id, languages),
          `Searching for "${item.title}" in the new languages`,
        ),
      clearObtained: () => run(() => clearObtainedWanted(), 'Removed obtained books'),
      loadAttempts: (item) => getWantedAttempts(item.id),
      unblockRelease: (item, attempt) =>
        run(
          () => unblockWantedRelease(item.id, attempt.id),
          `"${attempt.release_title ?? 'Release'}" may be tried again`,
        ),
      refresh,
    }),
    [
      enabled,
      canBookmark,
      isAdmin,
      items,
      loading,
      canBookmarkBook,
      bookLanguages,
      defaultLanguageCodes,
      stateFor,
      toggle,
      run,
      refresh,
    ],
  );

  return <WantedContext.Provider value={value}>{children}</WantedContext.Provider>;
}
