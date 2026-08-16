# Screen management & recording — implementation plan

Approved 2026-08-16. Delivers: group-based visibility for admins, and screen
recording with time-based playback.

## Decisions (from the user)

| Area | Decision |
| --- | --- |
| Admin visibility | **Group-based + individual exceptions** |
| Recording trigger | **Continuous** (always record assigned endpoints) |
| Recording format | **Keyframe + delta** (true video compression, H.264) vs **Full** |
| Retention | Configurable mechanism now; concrete days set later |

## Phase 1 — Groups & visibility ✅ DONE (2026-08-16)

Delivered: `endpoint_groups` / `endpoint_group_members` / `admin_group_assignments`
tables + `mode` on `admin_endpoint_scopes`; group-based `endpoint_ids_in_scope`;
`/api/groups` CRUD + members and `/api/users/<id>/scope`; console 群組 view +
per-admin 可見範圍 modal. 12 unit tests + 8 end-to-end DOM checks (incl. real
enforcement: an admin assigned to a group sees only that group's endpoints).

Endpoints are organised into groups; admins are assigned to groups; a non-super
admin sees the union of their groups' endpoints, plus individual includes, minus
individual excludes. SUPER_ADMIN sees everything (unchanged).

Data model:

```
endpoint_groups          (id, name, description)
endpoint_group_members   (group_id, endpoint_id)          many-to-many
admin_group_assignments  (user_id, group_id)              admin ↔ group
admin_endpoint_scopes    reframed: (user_id, endpoint_id, mode INCLUDE|EXCLUDE)
```

Visibility for a non-super admin:
`(∪ endpoints of assigned groups) ∪ individual-INCLUDE − individual-EXCLUDE`

Enforcement reuses the existing `endpoint_ids_in_scope` / `apply_endpoint_scope`
/ `can_access_endpoint`, so it applies uniformly to the endpoint list, live
screen viewing, and (later) recording access.

Managing groups and assignments is **SUPER_ADMIN only** — granting visibility is
a privilege change and must not be delegable to a regular admin.

## Phase 2 — Recording ✅ DONE (2026-08-16)

Delivered: FFmpeg H.264 encoding (local `server/tools/ffmpeg/ffmpeg.exe`);
`recording_policies` / `recording_segments`; per-endpoint `Recorder` +
`RecorderManager` (frames → segmented H.264 → AES-GCM encrypted files →
metadata index, plaintext deleted, frames never in DB); hub integration
(recording = a persistent virtual viewer, so the agent captures continuously);
`recording_control` starting/stopping recorders on agent connect/disconnect and
policy change; `/api/recordings` policy CRUD + segment listing (scoped, audited);
retention sweeper + `flask sweep-recordings`; console 錄影 view.
Verified end-to-end: the real agent recorded to encrypted DIFFERENTIAL segments
that decrypt to valid H.264. 9 unit tests + a DOM test that records via the UI.
Compression measured ~13% of raw JPEG. Screen data is always encrypted at rest:
the key comes from EEM_RECORDING_KEY, or (when unset) is auto-generated once and
kept in instance/recording.key, so recording works out of the box while never
writing plaintext. Set EEM_RECORDING_AUTO_KEY=0 to require an explicit key.

## Phase 2 — Recording (superseded by the note above)

- Agent gains a server-driven "record" mode: stream continuously regardless of
  whether a live viewer is connected.
- Server recording pipeline: JPEG frames → FFmpeg → H.264 fragmented-MP4
  segments (~5 min each). Differential = inter-frame (keyframe every N s + deltas);
  Full = all-intra / high bitrate.
- Segments encrypted at rest on disk (AES, key from environment/secret manager).
  **Frames never touch the database** (§14); only segment metadata is indexed.
- `recording_policies` (endpoint/group, mode, fps, retention_days, enabled) and
  `recording_segments` (endpoint, start/end, file, mode, size, sha256).
- Retention job deletes segments past their policy's retention.
- Dependency: **FFmpeg** (not yet installed).

## Phase 3 — Playback ✅ DONE (2026-08-16)

Delivered: `GET /api/recordings/segments/<id>/video` decrypts a segment and
streams it as MP4 (RBAC + endpoint scope + `VIEW_RECORDING` audit with the
segment's time span); console 回放 button per endpoint opens a modal with a date
picker, a timeline of the day's segments (gaps visible), and an HTML5 `<video>`
that fetches each segment with the auth header, plays it from a blob, and
auto-advances to the next. `media-src blob:` added to the CSP. Timeline paginates
so a full day of segments loads. Verified end-to-end: recorded segments → click a
block → fetch → server-side decrypt → valid H.264 plays → audit written.

## Standing constraints (CLAUDE.md)

- Continuous recording is a deliberate, audited exception to "capture only when
  watched" (§14). The deployment's written consent must cover **recording**, not
  just live viewing.
- Screen data: encrypted object/file storage + retention + auto-deletion
  (§14, §23, §24). Never in the database.
- Every record start/stop, policy change, and playback is audited (§17).
