import { useState } from 'react';

import { useWanted } from '../../contexts/WantedContext';
import { useDependencyEffect } from '../../hooks/useMountEffect';
import type { WantedAttempt, WantedItem } from '../../types';
import {
  LANGUAGE_OPTION_ALL,
  LANGUAGE_OPTION_DEFAULT,
  getLanguageFilterValues,
} from '../../utils/languageFilters';
import {
  WANTED_ATTEMPT_LABELS,
  WANTED_FILTERS,
  WANTED_FILTER_LABELS,
  WANTED_STATE_LABELS,
  describeAttemptBlock,
  describeWantedItem,
  filterAndSortWantedItems,
  formatRelativeTime,
  hasClearableObtained,
} from '../../utils/wanted';
import type { WantedFilter, WantedSort } from '../../utils/wanted';
import { DropdownList } from '../DropdownList';
import { LanguageMultiSelect } from '../LanguageMultiSelect';

const STATE_STYLES: Record<WantedItem['state'], string> = {
  waiting: 'bg-gray-500/10 text-gray-700 dark:bg-gray-400/10 dark:text-gray-300',
  searching: 'bg-sky-500/15 text-sky-700 dark:text-sky-300',
  downloading: 'bg-sky-500/15 text-sky-700 dark:text-sky-300',
  obtained: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  paused: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
};

const SORT_OPTIONS: { value: WantedSort; label: string }[] = [
  { value: 'added', label: 'Recently added' },
  { value: 'next_search', label: 'Next search' },
  { value: 'title', label: 'Title' },
];

const ActionButton = ({
  label,
  onClick,
  tone = 'default',
}: {
  label: string;
  onClick: () => void;
  tone?: 'default' | 'danger';
}) => (
  <button
    type="button"
    onClick={onClick}
    className={`text-xs hover:underline ${
      tone === 'danger' ? 'text-red-600 dark:text-red-400' : 'text-sky-600 dark:text-sky-400'
    }`}
  >
    {label}
  </button>
);

const AttemptRow = ({ item, attempt }: { item: WantedItem; attempt: WantedAttempt }) => {
  const { isAdmin, unblockRelease } = useWanted();
  const block = describeAttemptBlock(attempt);
  const details = [
    attempt.source_label ?? attempt.source,
    attempt.format?.toUpperCase(),
    attempt.size,
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <li className="py-1.5">
      <p className="truncate text-xs" title={attempt.release_title ?? undefined}>
        {attempt.release_title ?? 'Unknown release'}
      </p>
      <p className="text-[11px] opacity-70">
        {WANTED_ATTEMPT_LABELS[attempt.status]}
        {details && ` · ${details}`}
        {attempt.created_at && ` · ${formatRelativeTime(attempt.created_at) ?? ''}`}
      </p>
      {attempt.error_message && (
        <p className="text-[11px] break-words text-red-600/80 dark:text-red-400/80">
          {attempt.error_message}
        </p>
      )}
      {block && (
        <p className="flex flex-wrap gap-x-2 text-[11px] text-amber-700 dark:text-amber-300">
          {block}
          {isAdmin && (
            <ActionButton label="Allow again" onClick={() => void unblockRelease(item, attempt)} />
          )}
        </p>
      )}
    </li>
  );
};

const WantedHistory = ({ item }: { item: WantedItem }) => {
  const { loadAttempts } = useWanted();
  const [attempts, setAttempts] = useState<WantedAttempt[] | null>(null);
  const [failed, setFailed] = useState(false);

  // Reload whenever the item moves on, so new attempts and lifted blocks show.
  useDependencyEffect(() => {
    let cancelled = false;
    loadAttempts(item)
      .then((loaded) => {
        if (!cancelled) {
          setAttempts(loaded);
          setFailed(false);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setFailed(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [item.id, item.state, item.attempt_count, item.last_result]);

  if (failed) {
    return <p className="mt-1 text-xs opacity-60">Could not load the history.</p>;
  }
  if (attempts === null) {
    return <p className="mt-1 text-xs opacity-60">Loading history...</p>;
  }
  if (attempts.length === 0) {
    return <p className="mt-1 text-xs opacity-60">No release tried yet.</p>;
  }
  return (
    <ul className="mt-1 divide-y divide-[color-mix(in_srgb,var(--border-muted)_40%,transparent)]">
      {attempts.map((attempt) => (
        <AttemptRow key={attempt.id} item={item} attempt={attempt} />
      ))}
    </ul>
  );
};

const LanguageEditor = ({ item, onClose }: { item: WantedItem; onClose: () => void }) => {
  const { bookLanguages, defaultLanguageCodes, setLanguages } = useWanted();
  const [selection, setSelection] = useState<string[]>(
    item.languages && item.languages.length > 0 ? item.languages : [LANGUAGE_OPTION_DEFAULT],
  );

  const save = () => {
    const languages = getLanguageFilterValues(selection, bookLanguages, defaultLanguageCodes);
    void setLanguages(item, languages);
    onClose();
  };

  return (
    <div className="mt-2 space-y-2">
      <LanguageMultiSelect
        options={bookLanguages}
        value={selection}
        onChange={setSelection}
        defaultLanguageCodes={defaultLanguageCodes}
      />
      <div className="flex gap-3">
        <ActionButton label="Save" onClick={save} />
        <ActionButton label="Cancel" onClick={onClose} />
      </div>
    </div>
  );
};

const WantedRow = ({ item }: { item: WantedItem }) => {
  const { isAdmin, remove, searchNow, setPaused, clearBlocklist, bookLanguages } = useWanted();
  const [showHistory, setShowHistory] = useState(false);
  const [editingLanguages, setEditingLanguages] = useState(false);
  const subscribers = item.subscribers ?? [];
  // Without sign-in there are no usernames to show.
  const subscriberNames = subscribers
    .map((sub) => sub.username)
    .filter((name): name is string => Boolean(name));
  const languageNames = (item.languages ?? []).map((code) =>
    code === LANGUAGE_OPTION_ALL
      ? 'All languages'
      : (bookLanguages.find((lang) => lang.code === code)?.language ?? code),
  );

  return (
    <div className="flex gap-3 py-3">
      <div className="h-16 w-11 shrink-0 overflow-hidden rounded bg-gray-500/10">
        {item.preview && (
          <img src={item.preview} alt="" className="h-full w-full object-cover" loading="lazy" />
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium" title={item.title}>
              {item.title}
            </p>
            {item.author && <p className="truncate text-xs opacity-70">{item.author}</p>}
          </div>
          <span
            className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${STATE_STYLES[item.state]}`}
          >
            {WANTED_STATE_LABELS[item.state]}
          </span>
        </div>
        <p className="mt-1 text-xs opacity-70">{describeWantedItem(item)}</p>
        {languageNames.length > 0 && (
          <p className="mt-0.5 truncate text-xs opacity-60">In {languageNames.join(', ')}</p>
        )}
        {isAdmin && subscriberNames.length > 0 && (
          <p className="mt-0.5 truncate text-xs opacity-60">For {subscriberNames.join(', ')}</p>
        )}
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
          {item.state === 'waiting' && (
            <ActionButton label="Search now" onClick={() => void searchNow(item)} />
          )}
          {item.subscribed && item.state !== 'obtained' && !editingLanguages && (
            <ActionButton label="Languages" onClick={() => setEditingLanguages(true)} />
          )}
          {(item.attempt_count > 0 || showHistory) && (
            <ActionButton
              label={showHistory ? 'Hide history' : 'History'}
              onClick={() => setShowHistory((shown) => !shown)}
            />
          )}
          {isAdmin && item.state === 'waiting' && (
            <ActionButton label="Pause" onClick={() => void setPaused(item, true)} />
          )}
          {isAdmin && item.state === 'paused' && (
            <ActionButton label="Resume" onClick={() => void setPaused(item, false)} />
          )}
          {isAdmin && item.attempt_count > 0 && item.state !== 'obtained' && (
            <ActionButton label="Retry failed releases" onClick={() => void clearBlocklist(item)} />
          )}
          {item.subscribed && (
            <ActionButton label="Remove" tone="danger" onClick={() => void remove(item)} />
          )}
          {isAdmin && (!item.subscribed || subscribers.length > 1) && (
            <ActionButton
              label="Delete for everyone"
              tone="danger"
              onClick={() => void remove(item, 'all')}
            />
          )}
        </div>
        {editingLanguages && (
          <LanguageEditor item={item} onClose={() => setEditingLanguages(false)} />
        )}
        {showHistory && <WantedHistory item={item} />}
      </div>
    </div>
  );
};

/** Books bookmarked to download automatically, with where each one stands. */
export const WantedList = () => {
  const { items, loading, isAdmin, clearObtained } = useWanted();
  const [filter, setFilter] = useState<WantedFilter>('all');
  const [sort, setSort] = useState<WantedSort>('added');
  // Deleting every user's obtained books is a bulk action; ask once more.
  const [confirmingClear, setConfirmingClear] = useState(false);

  if (items.length === 0) {
    return (
      <p className="mt-8 text-center text-sm opacity-70">
        {loading
          ? 'Loading wanted list...'
          : 'No bookmarked books. Use the eye on a search result to add one.'}
      </p>
    );
  }

  const visible = filterAndSortWantedItems(items, filter, sort);

  return (
    <div>
      <div className="flex flex-wrap items-center gap-1.5 pb-2">
        {WANTED_FILTERS.map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => setFilter(key)}
            aria-pressed={filter === key}
            className={`rounded-full px-2.5 py-0.5 text-xs ${
              filter === key
                ? 'bg-sky-600 text-white dark:bg-sky-500'
                : 'bg-gray-500/10 hover:bg-gray-500/20'
            }`}
          >
            {WANTED_FILTER_LABELS[key]}
          </button>
        ))}
      </div>
      <div className="flex items-center justify-between gap-2 pb-1">
        <DropdownList
          options={SORT_OPTIONS}
          value={sort}
          onChange={(value) => {
            const picked = typeof value === 'string' ? value : value[0];
            const option = SORT_OPTIONS.find((candidate) => candidate.value === picked);
            if (option) {
              setSort(option.value);
            }
          }}
          widthClassName="w-40"
        />
        {hasClearableObtained(items, isAdmin) &&
          (confirmingClear ? (
            <span className="flex gap-3">
              <ActionButton
                label="Delete for everyone?"
                tone="danger"
                onClick={() => {
                  setConfirmingClear(false);
                  void clearObtained();
                }}
              />
              <ActionButton label="Cancel" onClick={() => setConfirmingClear(false)} />
            </span>
          ) : (
            <ActionButton
              label={isAdmin ? 'Delete obtained' : 'Remove obtained'}
              tone="danger"
              onClick={() => (isAdmin ? setConfirmingClear(true) : void clearObtained())}
            />
          ))}
      </div>
      {visible.length === 0 ? (
        <p className="mt-6 text-center text-sm opacity-70">No books in this view.</p>
      ) : (
        <div className="divide-y divide-[color-mix(in_srgb,var(--border-muted)_60%,transparent)]">
          {visible.map((item) => (
            <WantedRow key={item.id} item={item} />
          ))}
        </div>
      )}
    </div>
  );
};
