import type { WantedBookPayload } from '../services/api';
import type { Book, ContentType, WantedBookState, WantedItem, WantedState } from '../types';

/** Providers whose ids are not metadata books (manual search, source-backed browse). */
const NON_METADATA_PROVIDERS = new Set(['manual']);

export const wantedKey = (provider: string, providerId: string): string =>
  `${provider}:${providerId}`;

/** Whether a search result can be bookmarked for automatic download. */
export const isBookmarkable = (book: Book, contentType: ContentType): boolean =>
  contentType === 'ebook' &&
  Boolean(book.provider && book.provider_id) &&
  !NON_METADATA_PROVIDERS.has(book.provider ?? '') &&
  !book.source;

/** The book data the wanted list stores; null when the book cannot be bookmarked. */
export const buildWantedBookPayload = (book: Book): WantedBookPayload | null => {
  if (!book.provider || !book.provider_id) {
    return null;
  }
  return {
    title: book.title,
    author: book.author,
    authors: book.authors,
    provider: book.provider,
    provider_id: book.provider_id,
    isbn_13: book.isbn_13,
    isbn_10: book.isbn_10,
    year: book.year,
    preview: book.preview,
    series_name: book.series_name,
    series_position: book.series_position,
    series_count: book.series_count,
    subtitle: book.subtitle,
    language: book.language,
    source_url: book.source_url,
    search_title: book.search_title,
    search_author: book.search_author,
    titles_by_language: book.titles_by_language,
    content_type: 'ebook',
  };
};

/**
 * The viewer's bookmark state for a book: local toggles first, then the viewer's own
 * wanted items. What the search results reported only counts until the list has loaded;
 * after that it may be stale (removed in another tab, or by an admin).
 */
export const resolveWantedState = (
  book: Book,
  overrides: ReadonlyMap<string, WantedBookState | null>,
  ownItems: ReadonlyMap<string, WantedItem>,
  listLoaded: boolean,
): WantedBookState | undefined => {
  if (!book.provider || !book.provider_id) {
    return undefined;
  }
  const key = wantedKey(book.provider, book.provider_id);
  if (overrides.has(key)) {
    return overrides.get(key) ?? undefined;
  }
  const item = ownItems.get(key);
  if (item) {
    return { id: item.id, state: item.state, subscribed: true };
  }
  if (listLoaded) {
    return undefined;
  }
  return book.wanted?.subscribed ? book.wanted : undefined;
};

export const WANTED_STATE_LABELS: Record<WantedState, string> = {
  waiting: 'Waiting',
  searching: 'Searching',
  downloading: 'Downloading',
  obtained: 'Obtained',
  paused: 'Paused',
};

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/** "in 3 h", "5 min ago", "now" - coarse, like the activity cards. */
export const formatRelativeTime = (iso: string | null, now: number = Date.now()): string | null => {
  if (!iso) {
    return null;
  }
  const time = Date.parse(iso);
  if (Number.isNaN(time)) {
    return null;
  }
  const diff = time - now;
  const absolute = Math.abs(diff);
  if (absolute < MINUTE_MS) {
    return 'now';
  }
  let amount: string;
  if (absolute < HOUR_MS) {
    amount = `${Math.round(absolute / MINUTE_MS)} min`;
  } else if (absolute < DAY_MS) {
    amount = `${Math.round(absolute / HOUR_MS)} h`;
  } else {
    amount = `${Math.round(absolute / DAY_MS)} d`;
  }
  return diff > 0 ? `in ${amount}` : `${amount} ago`;
};

/** One line describing where a wanted item stands. */
export const describeWantedItem = (item: WantedItem, now: number = Date.now()): string => {
  if (item.state === 'obtained') {
    return item.obtained_reason === 'in_library' ? 'Already in your library' : 'Downloaded';
  }
  if (item.state === 'searching') {
    return 'Searching all sources';
  }
  if (item.state === 'downloading') {
    return item.last_result ?? 'Downloading';
  }
  if (item.state === 'paused') {
    return 'Paused by an admin';
  }
  const next = formatRelativeTime(item.next_search_at, now);
  const parts = [item.last_result, next ? `next search ${next}` : null].filter(Boolean);
  return parts.length > 0 ? parts.join(' · ') : 'Waiting for the first search';
};

/** Own items by book key, for marking search results. */
export const indexOwnWantedItems = (items: WantedItem[]): Map<string, WantedItem> =>
  new Map(
    items
      .filter((item) => item.subscribed)
      .map((item) => [wantedKey(item.provider, item.provider_id), item]),
  );
