import React from 'react';

const WARNING_WINDOW_MS = 72 * 60 * 60 * 1000; // 72h

// Expiry warning (docs #96, #75 EXPORT_RETENTION_WINDOW): computed by
// `GET /api/sessions/{sid}/cleanup-status`'s `export_expires_at` field
// (ISO datetime or null; never stored on the plain session document --
// it is derived on the fly from `updated_at` once the run is
// export-ready and Publish-Guard-clean). Renders nothing before that
// endpoint has ever returned a value, or on an older backend without it.
export default function ExpiryWarningPanel({ exportExpiresAt }) {
  if (!exportExpiresAt) return null;
  const expires = new Date(exportExpiresAt);
  if (Number.isNaN(expires.getTime())) return null;
  const msRemaining = expires.getTime() - Date.now();
  const expired = msRemaining <= 0;
  const soon = !expired && msRemaining <= WARNING_WINDOW_MS;
  if (!expired && !soon) {
    return (
      <div className="mt-4 text-[11px] text-ink-muted font-mono" data-testid="export-expiry-note">
        Export retained until {expires.toLocaleString()}.
      </div>
    );
  }
  return (
    <div className={`mt-4 border-l-2 pl-4 py-2 ${expired ? 'border-oxblood' : 'border-signal'}`}
         data-testid="export-expiry-warning">
      <div className={`kicker ${expired ? 'text-oxblood' : 'text-signal'}`}>
        {expired ? 'Export retention window has closed' : 'Export retention window closing soon'}
      </div>
      <div className="text-[12px] text-ink-2 mt-1">
        {expired
          ? `This export expired at ${expires.toLocaleString()} and may already have been cleaned up. Re-run the pipeline for a fresh export.`
          : `Download this export before ${expires.toLocaleString()} -- it will no longer be retained after that.`}
      </div>
    </div>
  );
}
