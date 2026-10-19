import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { AITaskReadiness } from "../../types";

/** Warn *before* generating that a task will fall back to a template.
 *
 * Until now the operator learned this afterwards, from a note under a finished
 * artifact — having already spent the wait and, if they had written one, having
 * had their custom prompt silently ignored. Worse, the advice was unactionable
 * for anyone who is not an admin: `/api/ai/providers` is admin-only, so an
 * editor could neither open the Settings page the notice pointed at nor see that
 * a key was missing in the first place.
 */
export function AiReadinessNotice({ tasks, onOpenSettings }: {
  tasks: string[];
  onOpenSettings?: () => void;
}) {
  const [readiness, setReadiness] = useState<AITaskReadiness[] | null>(null);

  useEffect(() => {
    let active = true;
    // Static server state, fetched once — the studio re-renders on every
    // keystroke and this must not become a request per character.
    api.getAIReadiness()
      .then((result) => active && setReadiness(result.tasks))
      // Readiness is advisory: failing to fetch it must never block generating.
      .catch(() => active && setReadiness([]));
    return () => { active = false; };
  }, []);

  if (!readiness?.length) return null;
  const relevant = readiness.filter((task) => tasks.includes(task.task_type));
  const falling_back = relevant.filter((task) => task.will_use_fallback);
  if (!falling_back.length) return null;

  return <p className="ai-readiness-notice" role="status">
    {falling_back.length === relevant.length
      ? "No AI provider is configured for this step, so it will produce template copy."
      : `${falling_back.length} of ${relevant.length} steps here have no AI provider and will produce template copy.`}
    {" "}A custom prompt or voice is not applied to template output.
    {onOpenSettings ? <> <button type="button" className="link-button" onClick={onOpenSettings}>Provider settings</button></> : null}
  </p>;
}
