# Changelog

## 1.2.0 - 2026-08-04

### Added

- Added a portable loading indicator for remote folder reads and file-preview downloads.
- Added a Transfers setting that connects one saved server automatically at launch.
- Restricted task cancellation to paused tasks, matching the transfer queue workflow.

## 1.1.0 - 2026-08-03

### Added

- Added portable checked-item selection and `Ctrl/Command-A` selection toggling.
- Added connection state display plus explicit connect/disconnect control.
- Added preview, transfer, and sync tabs to Settings.
- Added configurable standard-library text previews with safe size limits.
- Added transfer queue bulk start, pause, failed-task retry, and finished-task cleanup controls.

### Notes

- macOS-only AppKit, Finder, Keychain, local-network authorization, and native media preview behavior remain outside the Python portability boundary.
