import { useState } from 'react';

import { useWanted } from '../contexts/WantedContext';
import type { Book } from '../types';

interface WantedToggleButtonProps {
  book: Book;
  variant?: 'icon' | 'pill';
  className?: string;
}

export const EyeIcon = ({
  className = 'h-4 w-4',
  filled = false,
}: {
  className?: string;
  filled?: boolean;
}) => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    fill={filled ? 'currentColor' : 'none'}
    viewBox="0 0 24 24"
    strokeWidth="1.5"
    stroke={filled ? 'none' : 'currentColor'}
    aria-hidden="true"
    className={`${className} shrink-0`}
  >
    {filled ? (
      <>
        <path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z" />
        <path
          fillRule="evenodd"
          d="M1.323 11.447C2.811 6.976 7.028 3.75 12.001 3.75c4.97 0 9.185 3.223 10.675 7.69.12.362.12.752 0 1.113-1.487 4.471-5.705 7.697-10.677 7.697-4.97 0-9.186-3.223-10.675-7.69a1.762 1.762 0 0 1 0-1.113ZM17.25 12a5.25 5.25 0 1 1-10.5 0 5.25 5.25 0 0 1 10.5 0Z"
          clipRule="evenodd"
        />
      </>
    ) : (
      <>
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M2.036 12.322a1.012 1.012 0 0 1 0-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178Z"
        />
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"
        />
      </>
    )}
  </svg>
);

/** Bookmark a book so it downloads automatically once a release turns up. */
export const WantedToggleButton = ({
  book,
  variant = 'icon',
  className,
}: WantedToggleButtonProps) => {
  const { canBookmarkBook, stateFor, toggle } = useWanted();
  const [busy, setBusy] = useState(false);

  if (!canBookmarkBook(book)) {
    return null;
  }

  const wanted = stateFor(book)?.subscribed === true;
  const label = wanted ? 'Remove from wanted list' : 'Add to wanted list';
  const onClick = (event: React.MouseEvent) => {
    event.stopPropagation();
    if (busy) {
      return;
    }
    setBusy(true);
    void toggle(book).finally(() => setBusy(false));
  };

  if (variant === 'pill') {
    return (
      <button
        type="button"
        onClick={onClick}
        disabled={busy}
        aria-pressed={wanted}
        title={label}
        className={`inline-flex items-center gap-1 rounded-full px-2 py-1 text-xs font-medium transition-colors focus:outline-hidden disabled:opacity-60 ${
          wanted
            ? 'bg-sky-100 text-sky-700 hover:bg-sky-200 dark:bg-sky-900/40 dark:text-sky-300 dark:hover:bg-sky-900/60'
            : 'bg-sky-50 text-sky-600 hover:bg-sky-100 dark:bg-sky-900/20 dark:text-sky-400 dark:hover:bg-sky-900/40'
        } ${className ?? ''}`}
      >
        <EyeIcon className="h-3 w-3" filled={wanted} />
        {wanted ? 'Wanted' : 'Want'}
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      aria-pressed={wanted}
      aria-label={label}
      title={label}
      className={`flex items-center justify-center rounded-full transition-colors duration-200 focus:outline-hidden disabled:opacity-60 ${
        className ?? 'hover-action p-1.5 text-gray-600 sm:p-2 dark:text-gray-200'
      } ${wanted ? 'text-sky-600 dark:text-sky-400' : ''}`}
    >
      <EyeIcon className="h-4 w-4 sm:h-5 sm:w-5" filled={wanted} />
    </button>
  );
};

/** Always-visible marker for a book on the viewer's wanted list (the toggle may hide). */
export const WantedBadge = ({
  book,
  overlay = false,
  className = '',
}: {
  book: Book;
  overlay?: boolean;
  className?: string;
}) => {
  const { canBookmarkBook, stateFor } = useWanted();
  if (!canBookmarkBook(book) || stateFor(book)?.subscribed !== true) {
    return null;
  }
  const pill = overlay
    ? 'rounded-md border border-sky-700 bg-sky-600 px-1.5 py-0.5 text-[10px] font-bold text-white'
    : 'rounded-md bg-sky-600/15 px-1.5 py-0.5 text-[10px] font-semibold text-sky-700 dark:text-sky-300';
  const style = overlay
    ? { boxShadow: '0 2px 8px rgba(0, 0, 0, 0.4), 0 1px 3px rgba(0, 0, 0, 0.3)' }
    : undefined;
  return (
    <span
      className={`flex w-fit items-center gap-0.5 ${pill} ${className}`}
      style={style}
      title="On your wanted list"
      aria-label="On your wanted list"
    >
      <EyeIcon className="h-3 w-3" filled />
      Wanted
    </span>
  );
};
