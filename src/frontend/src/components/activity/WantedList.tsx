import { useWanted } from '../../contexts/WantedContext';
import type { WantedItem } from '../../types';
import { WANTED_STATE_LABELS, describeWantedItem } from '../../utils/wanted';

const STATE_STYLES: Record<WantedItem['state'], string> = {
  waiting: 'bg-gray-500/10 text-gray-700 dark:bg-gray-400/10 dark:text-gray-300',
  searching: 'bg-sky-500/15 text-sky-700 dark:text-sky-300',
  downloading: 'bg-sky-500/15 text-sky-700 dark:text-sky-300',
  obtained: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  paused: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
};

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

const WantedRow = ({ item }: { item: WantedItem }) => {
  const { isAdmin, remove, searchNow, setPaused, clearBlocklist } = useWanted();
  const subscribers = item.subscribers ?? [];
  // Without sign-in there are no usernames to show.
  const subscriberNames = subscribers
    .map((sub) => sub.username)
    .filter((name): name is string => Boolean(name));

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
        {isAdmin && subscriberNames.length > 0 && (
          <p className="mt-0.5 truncate text-xs opacity-60">For {subscriberNames.join(', ')}</p>
        )}
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
          {item.state === 'waiting' && (
            <ActionButton label="Search now" onClick={() => void searchNow(item)} />
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
      </div>
    </div>
  );
};

/** Books bookmarked to download automatically, with where each one stands. */
export const WantedList = () => {
  const { items, loading } = useWanted();

  if (items.length === 0) {
    return (
      <p className="mt-8 text-center text-sm opacity-70">
        {loading
          ? 'Loading wanted list...'
          : 'No bookmarked books. Use the eye on a search result to add one.'}
      </p>
    );
  }

  return (
    <div className="divide-y divide-[color-mix(in_srgb,var(--border-muted)_60%,transparent)]">
      {items.map((item) => (
        <WantedRow key={item.id} item={item} />
      ))}
    </div>
  );
};
