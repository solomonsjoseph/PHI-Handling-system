import React from 'react';

// Simple SVG spinner for pending state
export default function Spinner() {
  return (
    <svg className="animate-spin w-3.5 h-3.5 text-oxblood" viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeOpacity="0.25"/>
      <path d="M22 12a10 10 0 0 1-10 10" stroke="currentColor" strokeWidth="3" strokeLinecap="round"/>
    </svg>
  );
}
