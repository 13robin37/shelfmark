import { describe, expect, it } from 'vitest';

import type { Book, WantedAttempt, WantedBookState, WantedItem } from '../types/index';
import {
  buildWantedBookPayload,
  describeAttemptBlock,
  describeWantedItem,
  filterAndSortWantedItems,
  hasClearableObtained,
  formatRelativeTime,
  indexOwnWantedItems,
  isBookmarkable,
  resolveWantedState,
  wantedKey,
} from '../utils/wanted';

const book: Book = {
  id: 'hardcover:1',
  title: 'Dune',
  author: 'Frank Herbert',
  authors: ['Frank Herbert'],
  provider: 'hardcover',
  provider_id: '1',
  isbn_13: '9780441172719',
  titles_by_language: { de: 'Der Wüstenplanet' },
};

const item = (overrides: Partial<WantedItem> = {}): WantedItem => ({
  id: 7,
  title: 'Dune',
  author: 'Frank Herbert',
  preview: null,
  year: null,
  provider: 'hardcover',
  provider_id: '1',
  state: 'waiting',
  search_count: 1,
  last_search_at: null,
  next_search_at: null,
  last_result: null,
  obtained_at: null,
  obtained_reason: null,
  attempt_count: 0,
  created_at: null,
  subscriber_count: 1,
  subscribed: true,
  languages: null,
  release_date: null,
  ...overrides,
});

describe('isBookmarkable', () => {
  it('accepts metadata ebooks only', () => {
    expect(isBookmarkable(book, 'ebook')).toBe(true);
    expect(isBookmarkable(book, 'audiobook')).toBe(false);
    expect(isBookmarkable({ ...book, provider: 'manual' }, 'ebook')).toBe(false);
    expect(isBookmarkable({ ...book, source: 'direct_download' }, 'ebook')).toBe(false);
    expect(isBookmarkable({ ...book, provider_id: undefined }, 'ebook')).toBe(false);
  });
});

describe('buildWantedBookPayload', () => {
  it('keeps the fields the backend matches releases with', () => {
    const payload = buildWantedBookPayload(book);
    expect(payload).toMatchObject({
      title: 'Dune',
      provider: 'hardcover',
      provider_id: '1',
      isbn_13: '9780441172719',
      authors: ['Frank Herbert'],
      titles_by_language: { de: 'Der Wüstenplanet' },
      content_type: 'ebook',
    });
  });

  it('returns null without a provider id', () => {
    expect(buildWantedBookPayload({ ...book, provider_id: undefined })).toBeNull();
  });
});

describe('resolveWantedState', () => {
  const key = wantedKey('hardcover', '1');

  it('prefers a local toggle over everything else', () => {
    const overrides = new Map<string, WantedBookState | null>([[key, null]]);
    const own = indexOwnWantedItems([item()]);
    expect(resolveWantedState(book, overrides, own, new Map(), true)).toBeUndefined();
  });

  it('uses the viewer’s own items', () => {
    expect(
      resolveWantedState(book, new Map(), indexOwnWantedItems([item()]), new Map(), true),
    ).toEqual({
      id: 7,
      state: 'waiting',
      subscribed: true,
    });
  });

  it('trusts the search result only until the list has loaded', () => {
    const fromSearch = {
      ...book,
      wanted: { id: 9, state: 'paused' as const, subscribed: true },
    };
    const none = indexOwnWantedItems([]);
    expect(resolveWantedState(fromSearch, new Map(), none, new Map(), false)?.id).toBe(9);
    // Removed elsewhere since the search ran: the loaded list is the truth.
    expect(resolveWantedState(fromSearch, new Map(), none, new Map(), true)).toBeUndefined();
  });

  it('finds an item another provider’s book created, by its id', () => {
    const other = { ...book, provider: 'openlibrary', provider_id: 'OL1W' };
    const own = indexOwnWantedItems([item()]);
    const linked = new Map([[wantedKey('openlibrary', 'OL1W'), 7]]);
    expect(resolveWantedState(other, new Map(), own, linked, true)?.id).toBe(7);
    const reported = {
      ...other,
      wanted: { id: 7, state: 'waiting' as const, subscribed: true },
    };
    expect(resolveWantedState(reported, new Map(), own, new Map(), true)?.state).toBe('waiting');
    expect(resolveWantedState(other, new Map(), own, new Map(), true)).toBeUndefined();
  });

  it('ignores items the viewer did not bookmark', () => {
    const own = indexOwnWantedItems([item({ subscribed: false })]);
    const notMine = {
      ...book,
      wanted: { id: 9, state: 'waiting' as const, subscribed: false },
    };
    expect(resolveWantedState(notMine, new Map(), own, new Map(), false)).toBeUndefined();
  });
});

describe('formatRelativeTime', () => {
  const now = Date.parse('2026-10-19T12:00:00Z');

  it('rounds to minutes, hours and days', () => {
    expect(formatRelativeTime('2026-10-19T12:00:20+00:00', now)).toBe('now');
    expect(formatRelativeTime('2026-10-19T12:30:00+00:00', now)).toBe('in 30 min');
    expect(formatRelativeTime('2026-10-19T09:00:00+00:00', now)).toBe('3 h ago');
    expect(formatRelativeTime('2026-10-21T12:00:00+00:00', now)).toBe('in 2 d');
    expect(formatRelativeTime(null, now)).toBeNull();
    expect(formatRelativeTime('not a date', now)).toBeNull();
  });
});

describe('describeWantedItem', () => {
  const now = Date.parse('2026-10-19T12:00:00Z');

  it('describes each state', () => {
    expect(describeWantedItem(item({ state: 'obtained', obtained_reason: 'in_library' }))).toBe(
      'Already in your library',
    );
    expect(describeWantedItem(item({ state: 'obtained', obtained_reason: 'downloaded' }))).toBe(
      'Downloaded',
    );
    expect(describeWantedItem(item({ state: 'searching' }))).toBe('Searching all sources');
    expect(
      describeWantedItem(
        item({
          last_result: 'Searched 2 source(s), no matching release',
          next_search_at: '2026-10-19T18:00:00+00:00',
        }),
        now,
      ),
    ).toBe('Searched 2 source(s), no matching release · next search in 6 h');
    expect(describeWantedItem(item(), now)).toBe('Waiting for the first search');
  });
});

const ids = (list: WantedItem[]) => list.map((entry) => entry.id);

const attempt = (overrides: Partial<WantedAttempt>): WantedAttempt => ({
  id: 1,
  cycle: 1,
  source: 'libgen',
  release_title: 'Dune',
  format: 'epub',
  size: null,
  status: 'error',
  error_message: null,
  created_at: null,
  finished_at: null,
  blocked: false,
  blocked_until: null,
  ...overrides,
});

describe('filterAndSortWantedItems', () => {
  const items = [
    item({ id: 1, title: 'beta', state: 'waiting', created_at: '2026-10-01T00:00:00+00:00' }),
    item({
      id: 2,
      title: 'Alpha',
      state: 'obtained',
      created_at: '2026-10-03T00:00:00+00:00',
    }),
    item({
      id: 3,
      title: 'Gamma',
      state: 'searching',
      created_at: '2026-10-02T00:00:00+00:00',
      next_search_at: '2026-10-20T00:00:00+00:00',
    }),
  ];

  it('filters by state group', () => {
    expect(ids(filterAndSortWantedItems(items, 'missing', 'added'))).toEqual([3, 1]);
    expect(ids(filterAndSortWantedItems(items, 'obtained', 'added'))).toEqual([2]);
    expect(filterAndSortWantedItems(items, 'paused', 'added')).toEqual([]);
  });

  it('sorts by added, title and next search', () => {
    expect(ids(filterAndSortWantedItems(items, 'all', 'added'))).toEqual([2, 3, 1]);
    expect(ids(filterAndSortWantedItems(items, 'all', 'title'))).toEqual([2, 1, 3]);
    expect(ids(filterAndSortWantedItems(items, 'all', 'next_search'))[0]).toBe(3);
  });
});

describe('hasClearableObtained', () => {
  it('counts own obtained books, or every one for admins', () => {
    const others = [item({ state: 'obtained', subscribed: false })];
    expect(hasClearableObtained(others, false)).toBe(false);
    expect(hasClearableObtained(others, true)).toBe(true);
    expect(hasClearableObtained([item()], true)).toBe(false);
  });
});

describe('describeAttemptBlock', () => {
  it('says whether and how long a release is blocked', () => {
    expect(describeAttemptBlock(attempt({}))).toBeNull();
    expect(describeAttemptBlock(attempt({ blocked: true, status: 'rejected' }))).toBe('Blocked');
    expect(
      describeAttemptBlock(attempt({ blocked: true, blocked_until: '2026-11-12T00:00:00+00:00' })),
    ).toMatch(/^Blocked until /);
  });
});

describe('unreleased books', () => {
  it('says when a bookmarked book comes out', () => {
    const now = Date.parse('2026-10-19T12:00:00Z');
    expect(describeWantedItem(item({ release_date: '2027-03-12' }), now)).toMatch(
      /^Releases .*2027; searching starts a day before$/,
    );
    expect(describeWantedItem(item({ release_date: '2020-01-01' }), now)).toBe(
      'Waiting for the first search',
    );
  });

  it('passes the release date on when bookmarking', () => {
    expect(buildWantedBookPayload({ ...book, release_date: '2027-03-12' })?.release_date).toBe(
      '2027-03-12',
    );
  });
});
