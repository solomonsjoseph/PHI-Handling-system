import React from 'react';

// Deterministic mono progress bar — kept for dev logs
export function MonoProgress({ percent = 0, width = 40 }) {
  const filled = Math.max(0, Math.min(width, Math.round((percent / 100) * width)));
  const bar = '='.repeat(Math.max(0, filled - 1)) + (percent > 0 && percent < 100 ? '>' : '=').padEnd(1);
  const rest = ' '.repeat(Math.max(0, width - filled));
  return (
    <span className="progress-mono text-xs" data-testid="mono-progress">
      [{bar}{rest}] {String(Math.round(percent)).padStart(3, ' ')}%
    </span>
  );
}

// Editorial panel: hairline top rule + serif kicker
export function Panel({ title, cite, right, children, testId }) {
  return (
    <section className="mt-16" data-testid={testId}>
      <div className="rule-top pt-4 flex items-baseline justify-between">
        <div>
          <div className="kicker">{title}</div>
          {cite && <div className="text-[11px] text-ink-muted mt-1 italic">{cite}</div>}
        </div>
        <div>{right}</div>
      </div>
      <div className="mt-6">{children}</div>
    </section>
  );
}

// Primary button — oxblood on paper, understated
export function Btn({ children, variant = 'default', testId, disabled, onClick, type = 'button', size = 'md', className = '' }) {
  const sizes = { sm: 'h-8 px-3 text-[11px]', md: 'h-10 px-5 text-xs', lg: 'h-12 px-6 text-sm' };
  const styles = {
    default: 'bg-transparent border border-rule text-ink hover:border-ink transition-colors',
    primary: 'bg-oxblood border border-oxblood text-paper hover:bg-oxblood-2 transition-colors',
    ghost:   'bg-transparent text-ink-2 hover:text-oxblood underline underline-offset-4 decoration-rule hover:decoration-oxblood',
    danger:  'bg-transparent border border-oxblood text-oxblood hover:bg-oxblood hover:text-paper transition-colors',
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      data-testid={testId}
      className={`${sizes[size]} ${styles[variant] || styles.default} font-medium tracking-wider uppercase disabled:opacity-30 disabled:cursor-not-allowed ${className}`}
    >
      {children}
    </button>
  );
}

// Understated pill — flat, hairline border, no fill by default
export function Tag({ children, color = 'default', testId }) {
  const colors = {
    default: 'border-rule text-ink-2',
    ink:     'border-ink text-ink',
    accent:  'border-oxblood text-oxblood',
    accept:  'border-clean text-clean',
    reject:  'border-oxblood text-oxblood',
    signal:  'border-signal text-signal',
  };
  return (
    <span data-testid={testId} className={`inline-flex items-center px-2 h-5 border text-[10px] font-medium tracking-widest uppercase ${colors[color]}`}>
      {children}
    </span>
  );
}


// Display metric: serif kicker label over a large display value. Shared by
// the benchmark and corpus-verifier stat grids so the display-number
// typography has one source of truth.
export function Stat({ label, value, tone = 'ink', testId }) {
  const toneClass = tone === 'oxblood' ? 'text-oxblood' : 'text-ink';
  return (
    <div>
      <div className="kicker">{label}</div>
      <div className={`font-display text-display-md ${toneClass}`} data-testid={testId}>{value}</div>
    </div>
  );
}

// Custom hand-drawn tick checkbox — used in output selector
export function CheckCard({ checked, onChange, locked, title, blurb, testId }) {
  return (
    <label
      className={`block rule-top pt-5 pb-6 pr-6 cursor-pointer group ${locked ? 'opacity-100 cursor-default' : ''}`}
      data-testid={testId}
    >
      <div className="flex items-start gap-5">
        <div
          onClick={(e) => { if (locked) e.preventDefault(); }}
          className={`w-6 h-6 flex-shrink-0 border-2 flex items-center justify-center transition-colors
            ${checked ? 'bg-oxblood border-oxblood' : 'bg-transparent border-ink'}
            ${locked ? '' : 'group-hover:border-oxblood'}`}
        >
          {checked && (
            <svg viewBox="0 0 24 24" className="w-4 h-4 text-paper">
              <path d="M4 12 l5 5 l11 -12" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          )}
          <input
            type="checkbox"
            checked={checked}
            disabled={locked}
            onChange={e => !locked && onChange(e.target.checked)}
            className="sr-only"
          />
        </div>
        <div className="flex-1">
          <div className="font-display text-display-sm text-ink flex items-center gap-3">
            {title}
            {locked && <Tag color="ink">always included</Tag>}
          </div>
          {blurb && <div className="mt-2 text-body text-ink-2 max-w-xl">{blurb}</div>}
        </div>
      </div>
    </label>
  );
}
