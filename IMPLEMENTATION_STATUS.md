# Implementation Status: Mastering Revision Type Selection

## Completed (Backend)

### ✅ Phase 1: Database Schema
- [x] Added `requested_revision_type` field to Track model (`app/models/track.py`)
- [x] Created Alembic migration (`alembic/versions/d98c001d9130_add_requested_revision_type_to_tracks.py`)

### ✅ Phase 2: Backend API (Partial)
- [x] Updated `WorkflowTransitionRequest` schema with `revision_type` field and validation
- [x] Updated `TrackRead` schema to include `requested_revision_type`
- [x] Updated `execute_transition()` in `workflow_engine.py` to:
  - Accept `revision_type` parameter
  - Validate mastering revisions require a revision type
  - Set `track.requested_revision_type` appropriately
  - Clear field when not in revision step
- [x] Updated `/workflow/transition` endpoint to pass `revision_type`
- [x] Updated `build_track_read()` to include `requested_revision_type`
- [x] Updated `_finalize_source_version_upload()` to clear `requested_revision_type` after upload
- [x] Added upload validation in `_ensure_revision_upload_permission()` to block file uploads when `stem_files` is requested

### ⚠️ Missing (Backend)
- [ ] Add validation to `_ensure_external_source_link_permission()` to block external links when `source_audio` is requested
  - **Note**: This function doesn't exist in the current worktree. It exists in the main `backend/` directory but not here.
  - **Action**: This needs to be added when merging back to `dev` branch, or the worktree needs to be updated with the latest changes.

## Not Started (Frontend)

### Phase 3: Frontend Type Definitions
- [ ] Add `requested_revision_type` to Track interface in `frontend/src/types/index.ts`
- [ ] Update `performTransition` API call in `frontend/src/api/index.ts`

### Phase 4: Frontend UI - Revision Type Selection
- [ ] Add state variables to `WorkflowStepView.vue`:
  - `revisionTypeModalOpen`
  - `pendingRevisionDecision`
  - `selectedRevisionType`
- [ ] Add revision type selection modal component
- [ ] Implement `_willTransitionToMasteringRevision()` helper
- [ ] Update transition handler to show modal for mastering revisions
- [ ] Implement `confirmRevisionType()` and `performTransitionCall()`

### Phase 5: Frontend UI - Upload Adaptation
- [ ] Add `effectiveUploadMode` computed property
- [ ] Hide upload mode toggle for mastering revisions
- [ ] Show appropriate hint based on `requested_revision_type`
- [ ] Conditionally render upload interface

### Phase 6: i18n Translations
- [ ] Add Chinese translations to `frontend/src/locales/zh-CN.json`
- [ ] Add English translations to `frontend/src/locales/en.json`

### Phase 7: Testing
- [ ] Write backend tests for revision type validation
- [ ] Write frontend tests for modal and upload UI

### Phase 8: Documentation
- [ ] Update CHANGELOG

## Notes

- The worktree was created from an older version of `dev` that doesn't have the external source link submission feature
- When merging, need to ensure `_ensure_external_source_link_permission()` validation is added
- All backend core logic is complete except for the external link validation

## Next Steps

1. **Option A**: Continue in current worktree
   - Complete frontend implementation
   - Manually add external link validation when merging

2. **Option B**: Update worktree with latest `dev`
   - Merge or rebase current changes
   - Then add external link validation
   - Complete frontend implementation

3. **Option C**: Exit worktree and work in main `backend/`
   - Apply all backend changes to main backend directory
   - Complete frontend implementation
