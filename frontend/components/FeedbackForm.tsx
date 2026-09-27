"use client";

import { useState } from "react";

import { useOperatorIdentity } from "@/components/OperatorIdentityProvider";
import { useFeedback, useRcaPatterns, useSubmitFeedback } from "@/lib/queries/feedback";
import { RCA_STATUS_OPTIONS, humanizeRcaStatus } from "@/lib/rca";

const NO_CORRECTION = "";

/** Feedback comment + confidence score, plus the existing
 * thread beneath. `given_by` comes from the shared operator identity --
 * set once via the nav chip, not re-typed per form.
 *
 * Two additional optional fields -- "correct pattern" and
 * "correct RCA status" -- so feedback can carry a structured correction
 * instead of only free-text prose. Cleaner grounding for the feedback-RAG
 * loop: a structured correction is unambiguous, where a comment has
 * to be interpreted by the LLM at retrieval time. */
export function FeedbackForm({ executionId }: { executionId: string }) {
  const { name: givenBy } = useOperatorIdentity();
  const { data: feedback } = useFeedback(executionId);
  const { data: patterns } = useRcaPatterns();
  const submit = useSubmitFeedback(executionId);
  const [comment, setComment] = useState("");
  const [confidenceScore, setConfidenceScore] = useState(3);
  const [correctedPatternId, setCorrectedPatternId] = useState(NO_CORRECTION);
  const [correctedRcaStatus, setCorrectedRcaStatus] = useState(NO_CORRECTION);

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!givenBy) return;
    submit.mutate(
      {
        comment: comment.trim() || undefined,
        confidence_score: confidenceScore,
        given_by: givenBy,
        corrected_pattern_id: correctedPatternId || undefined,
        corrected_rca_status: correctedRcaStatus || undefined,
      },
      {
        onSuccess: () => {
          setComment("");
          setCorrectedPatternId(NO_CORRECTION);
          setCorrectedRcaStatus(NO_CORRECTION);
        },
      },
    );
  }

  return (
    <div>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3 rounded-md border border-grid bg-white p-4">
        <label className="text-xs text-muted">
          Comment
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            rows={2}
            className="mt-1 block w-full rounded border border-grid px-2 py-1 text-sm text-heading"
          />
        </label>
        <label className="text-xs text-muted">
          Confidence (1-5)
          <select
            value={confidenceScore}
            onChange={(e) => setConfidenceScore(Number(e.target.value))}
            className="mt-1 block w-20 rounded border border-grid px-2 py-1 text-sm text-heading"
          >
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="text-xs text-muted">
            Correct pattern (if different)
            <select
              value={correctedPatternId}
              onChange={(e) => setCorrectedPatternId(e.target.value)}
              className="mt-1 block w-full rounded border border-grid px-2 py-1 text-sm text-heading"
            >
              <option value={NO_CORRECTION}>(no correction)</option>
              {patterns?.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.id}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-muted">
            Correct RCA status (if different)
            <select
              value={correctedRcaStatus}
              onChange={(e) => setCorrectedRcaStatus(e.target.value)}
              className="mt-1 block w-full rounded border border-grid px-2 py-1 text-sm text-heading"
            >
              <option value={NO_CORRECTION}>(no correction)</option>
              {RCA_STATUS_OPTIONS.map((status) => (
                <option key={status} value={status}>
                  {humanizeRcaStatus(status)}
                </option>
              ))}
            </select>
          </label>
        </div>

        {submit.isError && <p className="text-xs text-danger">{(submit.error as Error).message}</p>}
        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={submit.isPending || !givenBy}
            className="rounded bg-action px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            {submit.isPending ? "Submitting…" : "Submit feedback"}
          </button>
          <span className="text-xs text-muted">as {givenBy ?? "…"}</span>
        </div>
      </form>

      {feedback && feedback.length > 0 && (
        <ul className="mt-3 space-y-2">
          {feedback.map((entry) => (
            <li key={entry.id} className="rounded border border-grid bg-white p-3 text-sm">
              <div className="flex items-center justify-between text-xs text-muted">
                <span>{entry.given_by}</span>
                <span>
                  confidence {entry.confidence_score}/5 &middot; {new Date(entry.created_at).toLocaleString()}
                </span>
              </div>
              {entry.comment && <p className="mt-1 text-heading">{entry.comment}</p>}
              {(entry.corrected_pattern_id || entry.corrected_rca_status) && (
                <p className="mt-1 text-xs text-highlight">
                  Operator flagged: should be{" "}
                  {entry.corrected_pattern_id && <span className="font-medium">{entry.corrected_pattern_id}</span>}
                  {entry.corrected_pattern_id && entry.corrected_rca_status && " / "}
                  {entry.corrected_rca_status && (
                    <span className="font-medium">{humanizeRcaStatus(entry.corrected_rca_status)}</span>
                  )}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
