/* ---------------------------------------------------------------------
 * ProjectPipeline — public page-level entry point.
 *
 * PATCH 5A refactor (Feb 2026):
 *   The monolithic 1900+ line implementation was split into:
 *     • /components/pipeline/PipelineBoard.jsx   — orchestration root
 *     • /components/pipeline/PipelineToolbar.jsx — header CTAs
 *     • /components/pipeline/PipelineFilters.jsx — sticky control bar
 *     • /components/pipeline/PipelineColumn.jsx  — single stage column
 *     • /components/pipeline/PipelineCard.jsx    — talent card
 *     • /components/pipeline/BulkActionBar.jsx   — floating action bar
 *     • /components/pipeline/FollowUpLane.jsx    — virtual read-only lane
 *     • /components/pipeline/PipelineEmptyState.jsx
 *     • /components/pipeline/BulkAddModal.jsx
 *     • /components/pipeline/TalentBrowserModal.jsx
 *     • /components/pipeline/TalentAvatar.jsx
 *     • /components/pipeline/constants.js
 *   Plus hooks:
 *     • /hooks/usePipelineData.js
 *     • /hooks/usePipelineFilters.js
 *     • /hooks/useBulkSelection.js
 *     • /hooks/usePipelineDnD.js
 *
 * This wrapper exists so existing imports (`pages/ProjectPipeline`) keep
 * working unchanged. All behaviour, styling, data-testids, and the
 * public API surface are preserved exactly.
 * ------------------------------------------------------------------- */
import PipelineBoard from "@/components/pipeline/PipelineBoard";

export default PipelineBoard;
