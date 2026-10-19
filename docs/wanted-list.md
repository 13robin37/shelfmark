# Wanted List

Bookmark an ebook that is not available yet, and Shelfmark downloads it on its own once a matching release turns up. Configure it in **Settings → Wanted List**; it is off by default.

## Using it

- Click the **eye** on a search result, in the book details or in the release dialog to bookmark a book. Click it again to remove your bookmark. Once nobody wants a book any more, a download still running for it is cancelled.
- The **Wanted** tab in the activity sidebar lists your bookmarks with their state, the last search result and when the next search runs. Filter it by state, sort it, and clear obtained books. **Search now** runs a search at the next opportunity.
- **History** shows every release tried for a book, why it failed and whether it is blocked.
- **Languages** sets the languages you want a book in; bookmarking from the release dialog takes its language filter. Changing them searches again right away.
- Admins see every bookmark and who wants it, and can pause, resume, retry failed and cancelled releases (all at once, or one with **Allow again** in the history) or delete an entry for everyone.

- In the release dialog of a book you bookmarked, each release the wanted list would pass over says why (another volume, wrong format, failed before, ...). Downloading a release there while the bookmark waits counts for the bookmark: it is delivered to everyone who bookmarked the book, like a release the wanted list found itself.

The eye only appears for ebooks from a metadata provider, and only for users who may download directly from at least one wanted-list source. Users whose policy requires a request do not see it.

## How it searches

1. A bookmarked book is searched within a few minutes, then again after growing intervals (**Search Intervals**, default 1, 6 and 24 hours). The last interval repeats for as long as the book stays bookmarked; a bookmark never gives up.
2. Each search asks every enabled source in **Sources** and ranks all releases together: releases must name the book (no other volume, sequel or bundle, no contradicting author), then **Preferred Formats** and the source order decide. Only enabled formats are downloaded; PDF and comic formats are off by default. With **Wait for Good Enough Format** set, lower formats are skipped for that many days after bookmarking, in case a release in the **Good Enough Format** (or a better one) turns up.
3. The best release is downloaded. If it fails, the next one is tried. A release that fails in two separate searches is skipped for 30 days. Cancelling a wanted download rejects that release until an admin clicks **Retry failed releases**.
4. Changing sources, mirrors, indexers, wanted-list sources or formats, or anyone's request policy or role makes every waiting bookmark search again at once, so a newly added source is tried immediately. A bookmark that no available source may serve for its users rests until one of these changes.
5. A book that is not out yet is not searched before the day before its release date, when the metadata provider knows it (Hardcover, Open Library, Google Books). The list shows when it comes out.
6. A book already in your library (see [Library Check](library-check.md)) is marked as obtained without downloading for users whose downloads go to a folder. Users receiving books by email or in Grimmory still get it delivered.

Background searches give way to searches users run themselves and are capped by **Max Searches per Hour**. IRC is off by default because its results are cached for 30 days.

Switching the wanted list off stops new searches. Downloads already running finish and are recorded; bookmarks are kept for when it is switched on again.

## Several users, one book

A book is searched and downloaded once, however many users bookmarked it. Users who share a destination (for example one Calibre library) all get that one download. Users with a different destination, email recipient, Grimmory library or a `{User}` naming template get their own copy of the same file, without downloading it again.

Everyone who receives the book gets a **Bookmarked book obtained** notification, and a **Bookmarked book found** one when a release starts downloading (both selectable per route under Notifications). Failed single attempts do not notify anyone; a failure on Shelfmark's side, such as a destination that is not writable, notifies the admin.
