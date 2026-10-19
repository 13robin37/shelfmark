import { useState } from 'react';

import { useWanted } from '../contexts/WantedContext';
import { useDependencyEffect } from '../hooks/useMountEffect';
import { getAdminUsers } from '../services/api';
import type { AdminUser } from '../services/api';
import type { WantedWatchTarget } from '../types';
import { isWatchedTarget } from '../utils/wanted';
import { DropdownList } from './DropdownList';
import { EyeIcon } from './WantedToggleButton';

const OWN = 'self';

/** Bookmark a whole series, author or list now, or watch it for new books. */
export const WantedWatchActions = ({ target }: { target: WantedWatchTarget }) => {
  const { enabled, canBookmark, isAdmin, watches, bookmarkAll, watch, unwatch } = useWanted();
  const [busy, setBusy] = useState(false);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [owner, setOwner] = useState(OWN);

  // Admins may act for another user (the Hardcover lists live in the admin's account).
  useDependencyEffect(() => {
    if (!isAdmin || !enabled) {
      return undefined;
    }
    let cancelled = false;
    getAdminUsers()
      .then((loaded) => {
        if (!cancelled) {
          setUsers(loaded);
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [isAdmin, enabled]);

  if (!enabled || !(canBookmark || isAdmin)) {
    return null;
  }

  const ownerId = owner === OWN ? null : Number(owner);
  const existing = watches.find(
    (entry) =>
      isWatchedTarget(entry, target) && (ownerId === null ? entry.own : entry.user_id === ownerId),
  );
  const act = (action: () => Promise<void>) => {
    if (busy) {
      return;
    }
    setBusy(true);
    void action().finally(() => setBusy(false));
  };

  return (
    <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
      <span className="flex items-center gap-1.5 opacity-80">
        <EyeIcon className="h-4 w-4" />
        Wanted list:
      </span>
      {(canBookmark || ownerId !== null) && (
        <button
          type="button"
          disabled={busy}
          onClick={() => act(() => bookmarkAll(target, ownerId))}
          className="rounded-full bg-gray-500/10 px-3 py-1 text-xs hover:bg-gray-500/20 disabled:opacity-50"
        >
          Bookmark all missing
        </button>
      )}
      {existing ? (
        <button
          type="button"
          disabled={busy}
          onClick={() => act(() => unwatch(existing))}
          className="rounded-full bg-sky-600 px-3 py-1 text-xs text-white hover:bg-sky-700 disabled:opacity-50"
        >
          Watching · stop
        </button>
      ) : (
        <button
          type="button"
          disabled={busy}
          onClick={() => act(() => watch(target, ownerId))}
          className="rounded-full bg-gray-500/10 px-3 py-1 text-xs hover:bg-gray-500/20 disabled:opacity-50"
        >
          Watch for new books
        </button>
      )}
      {isAdmin && users.length > 0 && (
        <DropdownList
          options={[
            { value: OWN, label: 'For me' },
            ...users.map((user) => ({ value: String(user.id), label: `For ${user.username}` })),
          ]}
          value={owner}
          onChange={(value) => setOwner(typeof value === 'string' ? value : (value[0] ?? OWN))}
          widthClassName="w-40"
        />
      )}
    </div>
  );
};
