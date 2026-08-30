import React from 'react';

// Security-incident-safe state (docs #96): `security_incident` is a real
// `RunState` value (`workflow.py`) and a real `FinalAssuranceGate` input
// (`security_incident_active`, docs #57) today, though no live orchestrator
// code path assigns it to `session.status` yet -- it exists as a
// `Session.status` value the moment a future phase wires it. This panel
// renders ONLY the generic, non-privileged-safe notice below and NEVER
// reads or displays any other session field while this state is active
// -- no raw incident detail, no gate internals, no trace content. This is
// intentional and absolute: there is no privileged-vs-non-privileged view
// distinction implemented in this console today, so every viewer is
// treated as non-privileged.
export default function SecurityIncidentNotice({ status }) {
  if (status !== 'security_incident') return null;
  return (
    <div className="mt-10 border-l-2 border-oxblood pl-4 py-3 bg-paper-2/50" data-testid="security-incident-notice">
      <div className="kicker text-oxblood">Security review in progress</div>
      <p className="text-[12px] text-ink-2 mt-2 leading-relaxed">
        This run has been paused for a security review. No further detail is shown here by
        design -- incident detail is restricted to the privileged security channel. No
        download is available while this review is open.
      </p>
    </div>
  );
}
