type ReviewIdentity = { target_path?: unknown; target_hash?: unknown; target_version_id?: unknown };
type CandidateIdentity = { result_path?: unknown; result_hash?: unknown; result_version_id?: unknown };

export function reviewMatchesCandidate(review: ReviewIdentity | null | undefined, candidate: CandidateIdentity | null | undefined): boolean {
  if (!review || !candidate) return false;
  return typeof review.target_path === "string" && review.target_path.length > 0
    && typeof review.target_hash === "string" && review.target_hash.length > 0
    && typeof review.target_version_id === "string" && review.target_version_id.length > 0
    && review.target_path === candidate.result_path
    && review.target_hash === candidate.result_hash
    && review.target_version_id === candidate.result_version_id;
}
