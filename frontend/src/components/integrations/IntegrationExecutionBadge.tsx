type Props = {
  executionPlane?: string;
  runnerStatus?: string | null;
  trustedInprocess?: boolean;
};

export default function IntegrationExecutionBadge({
  executionPlane,
  runnerStatus,
  trustedInprocess,
}: Props) {
  if (executionPlane === "container") {
    return <span className="ui-badge ui-badge--attention">Container runner · {runnerStatus ?? "never_seen"}</span>;
  }
  if (trustedInprocess) {
    return <span className="ui-badge ui-badge--critical">in-process (trusted)</span>;
  }
  return null;
}
